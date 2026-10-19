// The canonical source of the TypeScript code-analysis producer's analyser.
//
// `code_analysis_typescript.cjs` beside this file is its deterministic compiled form and
// is what actually runs; `scripts/generate-code-analysis-typescript.py --check` recompiles
// this file with the pinned compiler and fails if the two differ. Edit this file, never
// the `.cjs`, and regenerate it with the same script without `--check`.
//
// What it is: one process, one request, one answer. It reads a JSON request on stdin --
// the exact text of every admitted file of one sealed captured snapshot, already
// re-hashed by the Python adapter -- builds TypeScript `Program`s over an in-memory,
// restricted `CompilerHost`, asks each `TypeChecker` what every declaration and reference
// in the snapshot means, and writes one JSON answer on stdout. Positions are UTF-16 code
// unit offsets, the compiler's own; the adapter converts them to exact byte spans.
//
// What it never does: read the analysed repository (there is no repository here, only
// the request), touch `ts.sys`, emit, run a transformer or a language-service plugin,
// execute or `require` analysed source, read a project's JavaScript configuration,
// resolve a package outside the snapshot, or reach a network. The only file reads are
// the compiler's own `lib.*.d.ts` declarations, by validated basename, from the one
// directory the adapter names -- and the adapter starts Node under its permission model
// with only that directory and this file readable, so any read it did not intend
// fails rather than succeeds.
//
// Identity is syntactic and stable: a declaration is keyed by its repository-relative
// path and its qualified name, never by a compiler symbol or node id, so the same bytes
// key the same way in every Program, in every run, on every host.

import type * as TS from "typescript";

declare const require: (id: string) => unknown;
declare const process: {
  argv: string[];
  stdin: { setEncoding(encoding: string): void; on(event: string, listener: (chunk?: string) => void): void };
  stdout: { write(text: string): boolean };
  exitCode: number | undefined;
};

interface FsModule {
  readFileSync(path: string, encoding: "utf8"): string;
}

interface RequestFile {
  path: string;
  text: string;
}

interface RequestLimits {
  max_programs: number;
  max_extends_depth: number;
  max_references: number;
  max_ast_nodes_per_file: number;
  max_candidates: number;
  max_reports: number;
}

interface AnalysisRequest {
  format: string;
  request_digest: string;
  typescript_version: string;
  limits: RequestLimits;
  sources: RequestFile[];
  aux: RequestFile[];
}

interface NodeOut {
  key: string;
  kind: string;
  name: string;
  path: string;
  start: number;
  end: number;
}

interface EdgeOut {
  relationship: string;
  source: string;
  target: string | null;
  descriptor: string | null;
  status: string;
  strength: string;
  path: string;
  start: number;
  end: number;
}

interface ReportOut {
  path: string;
  outcome: string;
  detail: string;
  start: number | null;
  end: number | null;
}

interface DeclInfo {
  key: string;
  kind: string;
  name: string;
  start: number;
  end: number;
}

interface FileKeys {
  path: string;
  moduleKey: string | null;
  decls: Map<TS.Node, DeclInfo>;
  unsupported: number;
  firstUnsupported: [number, number] | null;
}

type Target =
  | { key: string; group: GroupOut | null }
  | { status: "unresolved" | "external" | "ambiguous"; descriptor: string; strength?: string };

interface GroupOut {
  key: string;
  kind: string;
  name: string;
  path: string;
  start: number;
  end: number;
  members: { key: string; path: string; start: number; end: number }[];
}

class Refusal extends Error {}

const REQUEST_FORMAT = "typescript_analysis_request.v1";
const RESPONSE_FORMAT = "typescript_analysis_response.v1";
const ROOT = "/snapshot";
const LIB = "/typescript-lib";
const LIB_NAME = /^lib(\.[a-z0-9]+)*\.d\.ts$/;
// The candidate contract's identifier pattern, `[\w$#][\w._:/@+$#-]{0,255}`, spelled
// for ECMAScript: Python's Unicode `\w` is letters, numbers and `_`.
const KEY = /^[\p{L}\p{N}_$#][\p{L}\p{N}_$#.:/@+-]{0,255}$/u;
const SEGMENT = /^[\p{L}\p{N}_$#]+$/u;
const MODULE_NAME = /^[\p{L}\p{N}_$#.:/@+-]+$/u;
const MAX_DESCRIPTOR = 1024;
const MAX_DETAIL = 200;
const CONFIG_NAMES = new Set(["tsconfig.json", "jsconfig.json"]);

function bounded(text: string, limit: number): string {
  return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
}

function compare(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function dirname(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "/" : path.slice(0, index);
}

function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

function main(): void {
  const ts = require(process.argv[2] + "/lib/typescript.js") as typeof TS;
  const fs = require("fs") as FsModule;
  const libDirectory = process.argv[2] + "/lib";
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    input += chunk ?? "";
  });
  process.stdin.on("end", () => {
    let request: AnalysisRequest;
    try {
      request = JSON.parse(input) as AnalysisRequest;
    } catch {
      process.stdout.write(JSON.stringify({ format: RESPONSE_FORMAT, refused: "request is not JSON" }));
      process.exitCode = 2;
      return;
    }
    try {
      process.stdout.write(JSON.stringify(analyse(ts, fs, libDirectory, request)));
    } catch (error) {
      const reason = error instanceof Refusal ? error.message : "analyser failed";
      process.stdout.write(
        JSON.stringify({ format: RESPONSE_FORMAT, request_digest: request.request_digest, refused: reason }),
      );
      process.exitCode = error instanceof Refusal ? 0 : 3;
    }
  });
}

function analyse(ts: typeof TS, fs: FsModule, libDirectory: string, request: AnalysisRequest): object {
  if (request.format !== REQUEST_FORMAT) throw new Refusal("unsupported request format");
  if (request.typescript_version !== ts.version) throw new Refusal("compiler version is not the pinned version");
  const limits = request.limits;

  // ---------------------------------------------------------------- in-memory files
  const texts = new Map<string, string>();
  const sources: string[] = [];
  for (const file of request.sources) {
    texts.set(ROOT + "/" + file.path, file.text);
    sources.push(file.path);
  }
  for (const file of request.aux) texts.set(ROOT + "/" + file.path, file.text);
  sources.sort(compare);
  const sourceSet = new Set(sources);
  const entries = new Map<string, { files: Set<string>; directories: Set<string> }>();
  const entry = (dir: string) => {
    let found = entries.get(dir);
    if (found === undefined) {
      found = { files: new Set(), directories: new Set() };
      entries.set(dir, found);
    }
    return found;
  };
  entry(ROOT);
  for (const full of texts.keys()) {
    let child = full;
    let parent = dirname(child);
    entry(parent).files.add(basename(child));
    while (parent !== ROOT && parent.startsWith(ROOT + "/")) {
      child = parent;
      parent = dirname(child);
      entry(parent).directories.add(basename(child));
    }
  }
  const libTexts = new Map<string, string | undefined>();
  const readText = (fileName: string): string | undefined => {
    if (fileName.startsWith(LIB + "/")) {
      const name = fileName.slice(LIB.length + 1);
      if (!LIB_NAME.test(name)) return undefined;
      if (!libTexts.has(name)) {
        let text: string | undefined;
        try {
          text = fs.readFileSync(libDirectory + "/" + name, "utf8");
        } catch (error) {
          if ((error as { code?: string }).code !== "ENOENT") throw error;
        }
        libTexts.set(name, text);
      }
      return libTexts.get(name);
    }
    return texts.get(fileName);
  };
  const directoryExists = (dir: string): boolean => entries.has(dir.endsWith("/") ? dir.slice(0, -1) : dir);
  const relative = (fileName: string): string | null =>
    fileName.startsWith(ROOT + "/") ? fileName.slice(ROOT.length + 1) : null;

  const host: TS.CompilerHost = {
    getSourceFile: (fileName, languageVersion) => {
      const text = readText(fileName);
      return text === undefined ? undefined : ts.createSourceFile(fileName, text, languageVersion, true);
    },
    getDefaultLibFileName: (options) => LIB + "/" + ts.getDefaultLibFileName(options),
    getDefaultLibLocation: () => LIB,
    writeFile: () => {
      throw new Refusal("emit was attempted");
    },
    getCurrentDirectory: () => ROOT,
    getCanonicalFileName: (fileName) => fileName,
    useCaseSensitiveFileNames: () => true,
    getNewLine: () => "\n",
    fileExists: (fileName) => readText(fileName) !== undefined,
    readFile: (fileName) => readText(fileName),
    directoryExists,
    getDirectories: (dir) => [...(entries.get(dir)?.directories ?? [])].sort(compare),
    realpath: (path) => path,
    getEnvironmentVariable: () => undefined,
    trace: () => undefined,
  };
  const matchFiles = (ts as unknown as Record<string, unknown>)["matchFiles"] as
    | ((
        path: string,
        extensions: readonly string[] | undefined,
        excludes: readonly string[] | undefined,
        includes: readonly string[] | undefined,
        useCaseSensitiveFileNames: boolean,
        currentDirectory: string,
        depth: number | undefined,
        getFileSystemEntries: (path: string) => { files: readonly string[]; directories: readonly string[] },
        realpath: (path: string) => string,
      ) => string[])
    | undefined;
  if (typeof matchFiles !== "function") throw new Refusal("compiler lacks its include matcher");
  const parseHost: TS.ParseConfigHost = {
    useCaseSensitiveFileNames: true,
    readDirectory: (root, extensions, excludes, includes, depth) =>
      matchFiles(root, extensions, excludes, includes, true, ROOT, depth, (dir) => {
        const found = entries.get(dir.endsWith("/") ? dir.slice(0, -1) : dir);
        return {
          files: [...(found?.files ?? [])].sort(compare),
          directories: [...(found?.directories ?? [])].sort(compare),
        };
      }, (path) => path),
    fileExists: (fileName) => readText(fileName) !== undefined,
    readFile: (fileName) => readText(fileName),
    trace: () => undefined,
  };

  // ---------------------------------------------------------------- coverage reports
  const reports: ReportOut[] = [];
  let problems = 0;
  const report = (path: string, outcome: string, detail: string, start: number | null, end: number | null) => {
    reports.push({ path, outcome, detail: bounded(detail, MAX_DETAIL), start, end });
    if (outcome !== "analysed") problems += 1;
    if (problems > limits.max_reports) throw new Refusal("snapshot produces more coverage reports than the profile admits");
  };
  const reportDiagnostic = (fallback: string, outcome: string, diagnostic: TS.Diagnostic) => {
    const file = diagnostic.file === undefined ? null : relative(diagnostic.file.fileName);
    const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, " ");
    const located = file !== null && diagnostic.start !== undefined;
    report(
      file ?? fallback,
      outcome,
      `TS${diagnostic.code}: ${message}`,
      located ? diagnostic.start! : null,
      located ? diagnostic.start! + (diagnostic.length ?? 0) : null,
    );
  };

  // ---------------------------------------------------------------- projects
  interface Project {
    config: string;
    options: TS.CompilerOptions;
    fileNames: string[];
  }
  const sanitise = (options: TS.CompilerOptions): TS.CompilerOptions => ({
    ...options,
    noEmit: true,
    emitDeclarationOnly: false,
    declaration: false,
    declarationMap: false,
    sourceMap: false,
    inlineSourceMap: false,
    composite: false,
    incremental: false,
    tsBuildInfoFile: undefined,
    plugins: undefined,
    traceResolution: false,
    generateTrace: undefined,
    diagnostics: false,
    extendedDiagnostics: false,
    noLib: options.noLib,
  });
  const roots = request.aux
    .map((file) => ROOT + "/" + file.path)
    .filter((path) => CONFIG_NAMES.has(basename(path)))
    .sort(compare);
  const queue = [...roots];
  const seen = new Set<string>(queue);
  const projects: Project[] = [];
  let followed = 0;
  while (queue.length > 0) {
    const config = queue.shift()!;
    const configPath = relative(config)!;
    const json = ts.parseJsonText(config, texts.get(config)!);
    const existing: TS.CompilerOptions =
      basename(config) === "jsconfig.json"
        ? { allowJs: true, maxNodeModuleJsDepth: 0, allowSyntheticDefaultImports: true, skipLibCheck: true }
        : {};
    const parsed = ts.parseJsonSourceFileConfigFileContent(json, parseHost, dirname(config), existing, config);
    // `parseDiagnostics` is the JSON/JSONC syntax verdict; the compiler keeps it off its
    // public declaration but sets it on every parsed file in the pinned version.
    const syntax = (json as unknown as { parseDiagnostics?: TS.Diagnostic[] }).parseDiagnostics ?? [];
    const reported = new Set<string>();
    for (const diagnostic of [...syntax, ...parsed.errors]) {
      const id = `${diagnostic.file?.fileName}\u0000${diagnostic.start}\u0000${diagnostic.code}`;
      if (reported.has(id)) continue;
      reported.add(id);
      reportDiagnostic(configPath, diagnostic.code === 18003 ? "config_empty" : "config_invalid", diagnostic);
    }
    const chain = (json as TS.TsConfigSourceFile).extendedSourceFiles ?? [];
    if (chain.length > limits.max_extends_depth) {
      report(configPath, "config_unsupported", `extends chain of ${chain.length} exceeds ${limits.max_extends_depth}`, null, null);
      continue;
    }
    if (parsed.options.plugins !== undefined) {
      report(configPath, "config_ignored", "compilerOptions.plugins are never loaded", null, null);
    }
    for (const reference of parsed.projectReferences ?? []) {
      const target = ts.resolveProjectReferencePath(reference);
      if (!texts.has(target)) {
        report(configPath, "reference_missing", `project reference ${relative(target) ?? "outside the snapshot"} is not in the snapshot`, null, null);
        continue;
      }
      if (seen.has(target)) continue;
      followed += 1;
      if (followed > limits.max_references) throw new Refusal("project references exceed the profile's bound");
      seen.add(target);
      queue.push(target);
    }
    projects.push({ config: configPath, options: sanitise(parsed.options), fileNames: [...parsed.fileNames].sort(compare) });
  }
  projects.sort((a, b) => compare(a.config, b.config));

  // Each source file is analysed in exactly one Program: the project whose directory is
  // its nearest ancestor among those that include it, else the first that includes it,
  // else the inferred Program. Every Program still sees every file its own project
  // includes, so resolution is always the owning project's resolution.
  const owner = new Map<string, Project | null>();
  for (const path of sources) {
    let best: Project | null = null;
    let bestDepth = -1;
    for (const project of projects) {
      if (!project.fileNames.includes(ROOT + "/" + path)) continue;
      const dir = dirname(project.config);
      const depth = dir === "/" || dir === "." || dir === "" ? 0 : path.startsWith(dir + "/") ? dir.length : -1;
      if (best === null || depth > bestDepth) {
        best = project;
        bestDepth = depth;
      }
    }
    owner.set(path, best);
  }
  const inferredOptions: TS.CompilerOptions = {
    allowJs: true,
    checkJs: false,
    jsx: ts.JsxEmit.Preserve,
    target: ts.ScriptTarget.ESNext,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    resolveJsonModule: true,
    allowImportingTsExtensions: true,
    strict: true,
    noEmit: true,
    skipLibCheck: true,
    types: [],
  };
  const plans: { config: string | null; options: TS.CompilerOptions; rootNames: string[]; analysed: string[] }[] = [];
  for (const project of projects) {
    const analysed = sources.filter((path) => owner.get(path) === project);
    if (analysed.length > 0) plans.push({ config: project.config, options: project.options, rootNames: project.fileNames, analysed });
  }
  const inferred = sources.filter((path) => owner.get(path) === null);
  if (inferred.length > 0) {
    plans.push({ config: null, options: inferredOptions, rootNames: inferred.map((path) => ROOT + "/" + path), analysed: inferred });
  }
  if (plans.length > limits.max_programs) throw new Refusal("snapshot needs more Programs than the profile admits");

  // ---------------------------------------------------------------- keys
  const fileKeys = new WeakMap<TS.SourceFile, FileKeys>();
  const memberName = (name: TS.Node): string | null => {
    if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name)) {
      return SEGMENT.test(name.text) ? name.text : null;
    }
    return null;
  };
  const isStatic = (node: TS.Node): boolean =>
    (ts.getCombinedModifierFlags(node as TS.Declaration) & ts.ModifierFlags.Static) !== 0;
  const topLevelBinding = (node: TS.Node): boolean => {
    let current: TS.Node = node;
    while (ts.isBindingElement(current) || ts.isObjectBindingPattern(current) || ts.isArrayBindingPattern(current)) {
      current = current.parent;
    }
    if (!ts.isVariableDeclaration(current)) return false;
    const statement = current.parent.parent;
    return ts.isVariableStatement(statement) && (ts.isSourceFile(statement.parent) || ts.isModuleBlock(statement.parent));
  };
  // What one node declares, as `[segment or null when unrepresentable, kind]`; or
  // undefined when the node is not a declaration this producer keys.
  const declarationOf = (node: TS.Node): [string | null, string] | undefined => {
    const parent = node.parent;
    if (ts.isFunctionDeclaration(node)) return [node.name ? memberName(node.name) : "default", "function"];
    if (ts.isClassDeclaration(node)) return [node.name ? memberName(node.name) : "default", "class"];
    if (ts.isInterfaceDeclaration(node)) return [memberName(node.name), "interface"];
    if (ts.isTypeAliasDeclaration(node)) return [memberName(node.name), "type_alias"];
    if (ts.isEnumDeclaration(node)) return [memberName(node.name), "enum"];
    if (ts.isEnumMember(node)) return [memberName(node.name), "enum_member"];
    if (ts.isModuleDeclaration(node)) {
      if ((node.flags & ts.NodeFlags.GlobalAugmentation) !== 0) return ["global:", "global_augmentation"];
      if (ts.isStringLiteral(node.name)) {
        return [MODULE_NAME.test(node.name.text) ? "module:" + node.name.text : null, "ambient_module"];
      }
      return [memberName(node.name), "namespace"];
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && topLevelBinding(node)) {
      return [memberName(node.name), "variable"];
    }
    if (ts.isBindingElement(node) && ts.isIdentifier(node.name) && topLevelBinding(node)) {
      return [memberName(node.name), "variable"];
    }
    if (parent !== undefined && ts.isClassDeclaration(parent)) {
      const kind = ts.isMethodDeclaration(node)
        ? "method"
        : ts.isPropertyDeclaration(node)
          ? "property"
          : ts.isGetAccessorDeclaration(node)
            ? "getter"
            : ts.isSetAccessorDeclaration(node)
              ? "setter"
              : null;
      if (kind !== null) {
        const name = memberName((node as TS.ClassElement).name!);
        return [name === null ? null : isStatic(node) ? "static:" + name : name, kind];
      }
      if (ts.isConstructorDeclaration(node)) return ["constructor", "constructor"];
    }
    if (parent !== undefined && ts.isInterfaceDeclaration(parent)) {
      if (ts.isMethodSignature(node)) return [memberName(node.name), "method"];
      if (ts.isPropertySignature(node)) return [memberName(node.name), "property"];
    }
    return undefined;
  };
  const keysOf = (sf: TS.SourceFile): FileKeys => {
    const cached = fileKeys.get(sf);
    if (cached !== undefined) return cached;
    const path = relative(sf.fileName)!;
    const result: FileKeys = {
      path,
      moduleKey: KEY.test(path) ? path : null,
      decls: new Map(),
      unsupported: 0,
      firstUnsupported: null,
    };
    const counts = new Map<string, number>();
    const unsupported = (node: TS.Node) => {
      result.unsupported += 1;
      if (result.firstUnsupported === null) result.firstUnsupported = [node.getStart(sf), node.end];
    };
    const declare = (node: TS.Node, segment: string | null, kind: string, prefix: string | null): string | null => {
      if (prefix === null || segment === null || result.moduleKey === null) {
        unsupported(node);
        return null;
      }
      const qualified = prefix === "" ? segment : prefix + "." + segment;
      const ordinal = (counts.get(qualified) ?? 0) + 1;
      counts.set(qualified, ordinal);
      const key = `${path}:${qualified}${ordinal > 1 ? "+" + ordinal : ""}`;
      if (!KEY.test(key)) {
        unsupported(node);
        return null;
      }
      result.decls.set(node, { key, kind, name: qualified, start: node.getStart(sf), end: node.end });
      return qualified;
    };
    const visit = (node: TS.Node, prefix: string | null, classPrefix: string | null): void => {
      let childPrefix = prefix;
      let childClass = classPrefix;
      const declared = declarationOf(node);
      if (declared !== undefined) {
        childPrefix = declare(node, declared[0], declared[1], prefix);
        if (ts.isClassDeclaration(node)) childClass = childPrefix;
      } else if (ts.isParameter(node) && ts.isConstructorDeclaration(node.parent) && ts.isParameterPropertyDeclaration(node, node.parent)) {
        declare(node, ts.isIdentifier(node.name) ? memberName(node.name) : null, "property", classPrefix);
      }
      ts.forEachChild(node, (child) => visit(child, childPrefix, childClass));
    };
    ts.forEachChild(sf, (child) => visit(child, "", null));
    fileKeys.set(sf, result);
    return result;
  };

  // ---------------------------------------------------------------- analysis
  const nodes = new Map<string, NodeOut>();
  const edges: EdgeOut[] = [];
  const groups = new Map<string, GroupOut>();
  const programs: { config: string | null; files: string[] }[] = [];
  const isSnapshot = (sf: TS.SourceFile) => relative(sf.fileName) !== null;
  const externalLabel = (sf: TS.SourceFile) => (isSnapshot(sf) ? relative(sf.fileName)! : "lib:" + basename(sf.fileName));

  for (const plan of plans) {
    const program = ts.createProgram({ rootNames: plan.rootNames, options: plan.options, host });
    const checker = program.getTypeChecker();
    programs.push({ config: plan.config, files: plan.analysed });
    for (const diagnostic of program.getOptionsDiagnostics()) {
      reportDiagnostic(plan.config ?? plan.analysed[0]!, "config_invalid", diagnostic);
    }
    const skipAlias = (symbol: TS.Symbol): TS.Symbol =>
      (symbol.flags & ts.SymbolFlags.Alias) !== 0 ? checker.getAliasedSymbol(symbol) : symbol;
    // A binding whose target has no value meaning is elided from the emitted module
    // whatever its syntax, so it is a type edge, never a runtime one. An unresolved
    // binding keeps its syntactic classification: nothing proves it is type-only.
    const typeMeaningOnly = (symbol: TS.Symbol | undefined): boolean => {
      if (symbol === undefined) return false;
      const target = skipAlias(symbol);
      return (target.declarations?.length ?? 0) > 0 && (target.flags & ts.SymbolFlags.Value) === 0;
    };
    const keyedDecl = (decl: TS.Node): DeclInfo | undefined => {
      const sf = decl.getSourceFile();
      if (!isSnapshot(sf)) return undefined;
      const keys = keysOf(sf);
      if (decl === sf) {
        return keys.moduleKey === null ? undefined : { key: keys.moduleKey, kind: "module", name: keys.path, start: 0, end: sf.text.length };
      }
      const direct = keys.decls.get(decl);
      if (direct !== undefined) return direct;
      if (
        (ts.isArrowFunction(decl) || ts.isFunctionExpression(decl) || ts.isClassExpression(decl)) &&
        ts.isVariableDeclaration(decl.parent) &&
        decl.parent.initializer === decl
      ) {
        return keys.decls.get(decl.parent);
      }
      return undefined;
    };
    const order = (a: TS.Node, b: TS.Node) =>
      compare(a.getSourceFile().fileName, b.getSourceFile().fileName) || a.pos - b.pos;
    const symbolLabel = (symbol: TS.Symbol) => bounded(checker.getFullyQualifiedName(symbol), 400);
    const symbolTarget = (raw: TS.Symbol): Target => {
      const symbol = skipAlias(raw);
      const decls = [...(symbol.declarations ?? [])].sort(order);
      if (decls.length === 0) return { status: "unresolved", descriptor: `no-declaration:${symbolLabel(symbol)}` };
      const first = decls[0]!;
      if (decls.length === 1 && ts.isSourceFile(first) && !sourceSet.has(relative(first.fileName) ?? "")) {
        return isSnapshot(first)
          ? { status: "unresolved", descriptor: `non-candidate-module:${externalLabel(first)}` }
          : { status: "external", descriptor: `module:${externalLabel(first)}` };
      }
      const inSnapshot = decls.filter((decl) => isSnapshot(decl.getSourceFile()));
      if (inSnapshot.length === 0) {
        return { status: "external", descriptor: `${externalLabel(first.getSourceFile())}:${symbolLabel(symbol)}` };
      }
      if (inSnapshot.length !== decls.length) {
        return { status: "external", descriptor: `augmented:${externalLabel(decls.find((d) => !isSnapshot(d.getSourceFile()))!.getSourceFile())}:${symbolLabel(symbol)}` };
      }
      const keyed = decls.map((decl) => [decl, keyedDecl(decl)] as const).filter((pair) => pair[1] !== undefined);
      if (keyed.length === 0) {
        return { status: "unresolved", descriptor: `non-candidate:${externalLabel(first.getSourceFile())}:${symbol.name}` };
      }
      if (decls.length === 1) return { key: keyed[0]![1]!.key, group: null };
      const [firstDecl, firstInfo] = keyed[0]!;
      const functionLike = decls.every((decl) => decl.kind === first.kind && ts.isFunctionLike(decl));
      const group: GroupOut = {
        key: firstInfo!.key + "@group",
        kind: functionLike ? "overload_group" : "merge_group",
        name: firstInfo!.name,
        path: relative(firstDecl.getSourceFile().fileName)!,
        start: firstInfo!.start,
        end: firstInfo!.end,
        members: keyed.map(([decl, info]) => ({
          key: info!.key,
          path: relative(decl.getSourceFile().fileName)!,
          start: info!.start,
          end: info!.end,
        })),
      };
      return { key: group.key, group };
    };

    for (const path of plan.analysed) {
      const sf = program.getSourceFile(ROOT + "/" + path);
      if (sf === undefined) throw new Refusal("an assigned file is missing from its Program");
      const syntax = program.getSyntacticDiagnostics(sf);
      if (syntax.length > 0) {
        const first = syntax[0]!;
        report(path, "syntax_invalid", `TS${first.code}: ${ts.flattenDiagnosticMessageText(first.messageText, " ")}`, first.start ?? 0, (first.start ?? 0) + (first.length ?? 0));
        continue;
      }
      let count = 0;
      const tally = (node: TS.Node): void => {
        count += 1;
        if (count <= limits.max_ast_nodes_per_file) ts.forEachChild(node, tally);
      };
      tally(sf);
      if (count > limits.max_ast_nodes_per_file) {
        report(path, "ast_budget_exceeded", `tree holds more than ${limits.max_ast_nodes_per_file} nodes`, null, null);
        continue;
      }
      const keys = keysOf(sf);
      if (keys.moduleKey === null) {
        report(path, "path_unsupported", "the path is outside the contract's identifier bounds", null, null);
        continue;
      }
      report(path, "analysed", plan.config === null ? "program: inferred" : `program: ${plan.config}`, null, null);
      if (keys.unsupported > 0) {
        report(
          path,
          "declaration_unsupported",
          `${keys.unsupported} declarations have no representable identity`,
          keys.firstUnsupported![0],
          keys.firstUnsupported![1],
        );
      }
      const moduleKey = keys.moduleKey;
      nodes.set(moduleKey, { key: moduleKey, kind: "module", name: path, path, start: 0, end: sf.text.length });

      const ownerOf = (node: TS.Node): string => {
        for (let current: TS.Node | undefined = node.parent; current !== undefined; current = current.parent) {
          const info = keys.decls.get(current);
          if (info !== undefined) return info.key;
        }
        return moduleKey;
      };
      const seenRefs = new Set<string>();
      const edge = (
        relationship: string,
        at: TS.Node,
        target: Target,
        strength: string,
        dedupe = false,
      ) => {
        const source = ownerOf(at);
        if ("key" in target) {
          if (target.group !== null) {
            const known = groups.get(target.group.key);
            if (known === undefined) groups.set(target.group.key, target.group);
            else for (const member of target.group.members) if (!known.members.some((m) => m.key === member.key)) known.members.push(member);
          }
          if (dedupe) {
            const id = `${relationship}\u0000${source}\u0000${target.key}`;
            if (seenRefs.has(id)) return;
            seenRefs.add(id);
          }
          edges.push({ relationship, source, target: target.key, descriptor: null, status: "resolved", strength, path, start: at.getStart(sf), end: at.end });
        } else {
          if (dedupe) {
            const id = `${relationship}\u0000${source}\u0000${target.descriptor}`;
            if (seenRefs.has(id)) return;
            seenRefs.add(id);
          }
          edges.push({
            relationship,
            source,
            target: null,
            descriptor: bounded(target.descriptor, MAX_DESCRIPTOR),
            status: target.status,
            strength: target.strength ?? (target.status === "external" ? strength : "syntactic"),
            path,
            start: at.getStart(sf),
            end: at.end,
          });
        }
      };

      // Declarations, containment and declared groups.
      for (const [decl, info] of keys.decls) {
        nodes.set(info.key, { key: info.key, kind: info.kind, name: info.name, path, start: info.start, end: info.end });
        edges.push({ relationship: "defines", source: ownerOf(decl), target: info.key, descriptor: null, status: "resolved", strength: "syntactic", path, start: info.start, end: info.end });
        const named = (decl as { name?: TS.Node }).name;
        const symbol = named !== undefined ? checker.getSymbolAtLocation(named) : undefined;
        const declared = symbol ?? (decl as unknown as { symbol?: TS.Symbol }).symbol;
        if (declared !== undefined && (declared.declarations?.length ?? 0) > 1) {
          const target = symbolTarget(declared);
          if ("key" in target && target.group !== null) {
            const known = groups.get(target.group.key);
            if (known === undefined) groups.set(target.group.key, target.group);
            else for (const member of target.group.members) if (!known.members.some((m) => m.key === member.key)) known.members.push(member);
          }
        }
      }

      const moduleTarget = (specifier: TS.Expression): Target => {
        const text = ts.isStringLiteralLike(specifier) ? specifier.text : null;
        if (text === null) return { status: "unresolved", descriptor: "dynamic-module-specifier", strength: "inferred_dynamic" };
        const symbol = checker.getSymbolAtLocation(specifier);
        if (symbol === undefined) {
          return /^\.{0,2}\//.test(text)
            ? { status: "unresolved", descriptor: `module-not-found:${text}` }
            : { status: "external", descriptor: `package:${text}` };
        }
        return symbolTarget(symbol);
      };
      const bindingTarget = (name: TS.Node, specifier: TS.Expression | undefined): Target => {
        const alias = checker.getSymbolAtLocation(name);
        if (alias === undefined) return { status: "unresolved", descriptor: `unbound-name:${name.getText(sf)}` };
        const target = skipAlias(alias);
        if ((target.declarations?.length ?? 0) === 0) {
          const module = specifier === undefined ? null : moduleTarget(specifier);
          if (module !== null && !("key" in module)) return { status: module.status, descriptor: `${module.descriptor}#${name.getText(sf)}` };
          return { status: "unresolved", descriptor: `missing-export:${name.getText(sf)}` };
        }
        return symbolTarget(target);
      };
      const strip = (expression: TS.Expression): TS.Expression => {
        let current = expression;
        while (ts.isParenthesizedExpression(current) || ts.isNonNullExpression(current)) current = current.expression;
        return current;
      };
      const isVirtual = (decl: TS.Node, receiver: TS.Expression | null): boolean => {
        if (receiver !== null && receiver.kind === ts.SyntaxKind.SuperKeyword) return false;
        if (!(ts.isMethodDeclaration(decl) || ts.isPropertyDeclaration(decl) || ts.isGetAccessorDeclaration(decl) || ts.isMethodSignature(decl) || ts.isPropertySignature(decl))) {
          return false;
        }
        const name = (decl as TS.NamedDeclaration).name;
        if (name !== undefined && ts.isPrivateIdentifier(name)) return false;
        const flags = ts.getCombinedModifierFlags(decl as TS.Declaration);
        return (flags & (ts.ModifierFlags.Static | ts.ModifierFlags.Private)) === 0;
      };
      const callTarget = (call: TS.CallExpression | TS.NewExpression | TS.TaggedTemplateExpression | TS.Decorator): [Target, string] => {
        const raw = ts.isTaggedTemplateExpression(call) ? call.tag : call.expression;
        const callee = strip(raw);
        if (callee.kind === ts.SyntaxKind.SuperKeyword) {
          const decl = checker.getResolvedSignature(call as TS.CallExpression)?.declaration;
          const info = decl === undefined ? undefined : keyedDecl(decl);
          if (info !== undefined) return [{ key: info.key, group: null }, "resolved_static"];
          const base = checker.getSymbolAtLocation(callee);
          return [base === undefined ? { status: "unresolved", descriptor: "super-call" } : symbolTarget(base), "resolved_static"];
        }
        let name: TS.Node;
        let receiver: TS.Expression | null = null;
        if (ts.isIdentifier(callee)) name = callee;
        else if (ts.isPropertyAccessExpression(callee)) {
          name = callee.name;
          receiver = strip(callee.expression);
        } else if (ts.isElementAccessExpression(callee) && ts.isStringLiteralLike(callee.argumentExpression)) {
          name = callee.argumentExpression;
          receiver = strip(callee.expression);
        } else {
          return [{ status: "unresolved", descriptor: `dynamic-call:${ts.SyntaxKind[callee.kind]}`, strength: "inferred_dynamic" }, "inferred_dynamic"];
        }
        const rawSymbol = checker.getSymbolAtLocation(name);
        if (rawSymbol === undefined) {
          const type = checker.getTypeAtLocation(callee);
          if ((type.flags & ts.TypeFlags.Any) !== 0 && receiver !== null) {
            return [{ status: "unresolved", descriptor: `untyped-call:${bounded(callee.getText(sf), 200)}`, strength: "inferred_dynamic" }, "inferred_dynamic"];
          }
          return [{ status: "unresolved", descriptor: `unbound-name:${bounded(name.getText(sf), 200)}` }, "syntactic"];
        }
        const symbol = skipAlias(rawSymbol);
        const decls = symbol.declarations ?? [];
        if (receiver !== null && decls.length > 1 && checker.getTypeAtLocation(receiver).isUnion()) {
          const parents = new Set(decls.map((decl) => decl.parent));
          if (parents.size > 1) {
            const labels = [...new Set(decls.map((decl) => keyedDecl(decl)?.key ?? externalLabel(decl.getSourceFile()) + ":" + symbol.name))].sort(compare);
            return [{ status: "ambiguous", descriptor: `union-dispatch:${labels.join(",")}`, strength: "inferred_dynamic" }, "inferred_dynamic"];
          }
        }
        const first = decls[0];
        if (first !== undefined && (ts.isVariableDeclaration(first) || ts.isParameter(first) || ts.isPropertySignature(first) || ts.isPropertyDeclaration(first) || ts.isBindingElement(first) || ts.isPropertyAssignment(first))) {
          const init = ts.isVariableDeclaration(first) || ts.isPropertyDeclaration(first) ? first.initializer : undefined;
          const functionValued = init !== undefined && (ts.isArrowFunction(strip(init)) || ts.isFunctionExpression(strip(init)) || ts.isClassExpression(strip(init)));
          const constant = ts.isVariableDeclaration(first) && (ts.getCombinedNodeFlags(first) & ts.NodeFlags.Const) !== 0;
          if (decls.length === 1 && functionValued && (constant || ts.isPropertyDeclaration(first)) && isSnapshot(first.getSourceFile())) {
            const target = symbolTarget(symbol);
            return [target, isVirtual(first, receiver) ? "inferred_dynamic" : "resolved_static"];
          }
          if (!isSnapshot(first.getSourceFile())) return [symbolTarget(symbol), "resolved_static"];
          return [{ status: "unresolved", descriptor: `indirect-call:${bounded(symbolLabel(symbol), 400)}`, strength: "inferred_dynamic" }, "inferred_dynamic"];
        }
        const chosen = checker.getResolvedSignature(call)?.declaration;
        if (chosen !== undefined && decls.includes(chosen as TS.Declaration)) {
          const info = keyedDecl(chosen);
          if (info !== undefined) return [{ key: info.key, group: null }, isVirtual(chosen, receiver) ? "inferred_dynamic" : "resolved_static"];
        }
        if (chosen !== undefined && ts.isConstructorDeclaration(chosen) && ts.isNewExpression(call)) {
          const owner = checker.getSymbolAtLocation(chosen.parent.name ?? chosen.parent);
          if (owner !== undefined && owner === symbol) {
            const info = keyedDecl(chosen);
            if (info !== undefined) return [{ key: info.key, group: null }, "resolved_static"];
          }
        }
        const target = symbolTarget(symbol);
        const virtual = first !== undefined && isVirtual(first, receiver);
        return [target, virtual ? "inferred_dynamic" : "resolved_static"];
      };

      const isJs = /\.(c|m)?jsx?$/.test(path);
      const visit = (node: TS.Node): void => {
        if (ts.isImportDeclaration(node)) {
          const clause = node.importClause;
          if (clause === undefined) {
            edge("imports", node, moduleTarget(node.moduleSpecifier), "resolved_static");
            return;
          }
          const typeOnly = clause.isTypeOnly;
          const imported = (name: TS.Identifier, syntactic: boolean) =>
            syntactic || typeMeaningOnly(checker.getSymbolAtLocation(name)) ? "type_imports" : "imports";
          if (clause.name !== undefined) edge(imported(clause.name, typeOnly), clause.name, bindingTarget(clause.name, node.moduleSpecifier), "resolved_static");
          const bindings = clause.namedBindings;
          if (bindings !== undefined && ts.isNamespaceImport(bindings)) {
            edge(typeOnly ? "type_imports" : "imports", bindings, moduleTarget(node.moduleSpecifier), "resolved_static");
          } else if (bindings !== undefined) {
            for (const element of bindings.elements) {
              edge(imported(element.name, typeOnly || element.isTypeOnly), element, bindingTarget(element.name, node.moduleSpecifier), "resolved_static");
            }
          }
          return;
        }
        if (ts.isExportDeclaration(node)) {
          const from = node.moduleSpecifier;
          const relationship = (from === undefined ? "exports" : "reexports");
          const typed = (typeOnly: boolean) => (typeOnly ? "type_" + relationship : relationship);
          const clause = node.exportClause;
          if (clause === undefined || ts.isNamespaceExport(clause)) {
            if (from !== undefined) edge(typed(node.isTypeOnly), clause ?? node, moduleTarget(from), "resolved_static");
            return;
          }
          for (const element of clause.elements) {
            const local = checker.getExportSpecifierLocalTargetSymbol(element);
            const target: Target =
              local === undefined
                ? from !== undefined
                  ? (() => {
                      const module = moduleTarget(from);
                      return "key" in module
                        ? { status: "unresolved" as const, descriptor: `missing-export:${(element.propertyName ?? element.name).getText(sf)}` }
                        : { status: module.status, descriptor: `${module.descriptor}#${(element.propertyName ?? element.name).getText(sf)}` };
                    })()
                  : { status: "unresolved", descriptor: `unbound-name:${element.name.getText(sf)}` }
                : symbolTarget(local);
            edge(typed(node.isTypeOnly || element.isTypeOnly || typeMeaningOnly(local)), element, target, "resolved_static");
          }
          return;
        }
        if (ts.isImportEqualsDeclaration(node)) {
          const reference = node.moduleReference;
          if (ts.isExternalModuleReference(reference)) {
            edge(node.isTypeOnly ? "type_imports" : "imports", node, moduleTarget(reference.expression), "resolved_static");
          } else {
            const symbol = checker.getSymbolAtLocation(reference);
            edge("aliases", node, symbol === undefined ? { status: "unresolved", descriptor: `unbound-name:${reference.getText(sf)}` } : symbolTarget(symbol), "resolved_static");
          }
          return;
        }
        if (ts.isExportAssignment(node)) {
          const expression = strip(node.expression);
          if (ts.isIdentifier(expression) || ts.isPropertyAccessExpression(expression)) {
            const symbol = checker.getSymbolAtLocation(expression);
            edge("exports", node, symbol === undefined ? { status: "unresolved", descriptor: `unbound-name:${bounded(expression.getText(sf), 200)}` } : symbolTarget(symbol), "resolved_static");
            if (ts.isPropertyAccessExpression(expression)) visit(expression.expression);
            return;
          }
        }
        if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
          const argument = node.arguments[0];
          if (argument !== undefined && ts.isStringLiteralLike(argument)) edge("dynamic_imports", node, moduleTarget(argument), "resolved_static");
          else edge("dynamic_imports", node, { status: "unresolved", descriptor: "dynamic-module-specifier", strength: "inferred_dynamic" }, "inferred_dynamic");
          node.arguments.forEach(visit);
          return;
        }
        if (
          isJs &&
          ts.isCallExpression(node) &&
          ts.isIdentifier(node.expression) &&
          node.expression.text === "require" &&
          node.arguments.length === 1 &&
          ts.isStringLiteralLike(node.arguments[0]!)
        ) {
          edge("imports", node, moduleTarget(node.arguments[0]!), "resolved_static");
          return;
        }
        if (ts.isCallExpression(node) || ts.isNewExpression(node) || ts.isTaggedTemplateExpression(node) || ts.isDecorator(node)) {
          const [target, strength] = callTarget(node);
          edge(ts.isNewExpression(node) ? "constructs" : "calls", node, target, strength);
          const callee = strip(ts.isTaggedTemplateExpression(node) ? node.tag : node.expression);
          if (ts.isPropertyAccessExpression(callee) || ts.isElementAccessExpression(callee)) visit(callee.expression);
          else if (!ts.isIdentifier(callee)) visit(callee);
          if (ts.isElementAccessExpression(callee)) visit(callee.argumentExpression);
          if (ts.isTaggedTemplateExpression(node)) visit(node.template);
          else if (!ts.isDecorator(node)) {
            node.typeArguments?.forEach(visit);
            node.arguments?.forEach(visit);
          }
          return;
        }
        if (ts.isHeritageClause(node)) {
          const relationship = node.token === ts.SyntaxKind.ExtendsKeyword ? "extends" : "implements";
          for (const type of node.types) {
            const expression = strip(type.expression);
            if (ts.isIdentifier(expression) || ts.isPropertyAccessExpression(expression)) {
              const symbol = checker.getSymbolAtLocation(expression);
              edge(relationship, type, symbol === undefined ? { status: "unresolved", descriptor: `unbound-name:${bounded(expression.getText(sf), 200)}` } : symbolTarget(symbol), "resolved_static");
              if (ts.isPropertyAccessExpression(expression)) visit(expression.expression);
            } else {
              edge(relationship, type, { status: "unresolved", descriptor: "dynamic-base", strength: "inferred_dynamic" }, "inferred_dynamic");
              visit(expression);
            }
            type.typeArguments?.forEach(visit);
          }
          return;
        }
        if (ts.isTypeReferenceNode(node) || ts.isTypeQueryNode(node)) {
          const name = ts.isTypeReferenceNode(node) ? node.typeName : node.exprName;
          const symbol = checker.getSymbolAtLocation(name);
          if (symbol === undefined) {
            edge("type_references", node, { status: "unresolved", descriptor: `unbound-name:${bounded(name.getText(sf), 200)}` }, "syntactic", true);
          } else {
            const target = symbolTarget(symbol);
            if ("key" in target || target.status !== "unresolved" || !target.descriptor.startsWith("non-candidate:")) {
              edge("type_references", node, target, "resolved_static", true);
            }
          }
          node.typeArguments?.forEach(visit);
          return;
        }
        if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && !(ts.isIdentifier(node.tagName) && /^[a-z]/.test(node.tagName.text))) {
          const symbol = checker.getSymbolAtLocation(node.tagName);
          if (symbol !== undefined) edge("renders", node.tagName, symbolTarget(symbol), "resolved_static", true);
          node.typeArguments?.forEach(visit);
          visit(node.attributes);
          return;
        }
        if (ts.isPropertyAccessExpression(node)) {
          const symbol = checker.getSymbolAtLocation(node.name);
          if (symbol !== undefined) {
            const target = symbolTarget(symbol);
            if ("key" in target) edge("references", node, target, "resolved_static", true);
          }
          visit(node.expression);
          return;
        }
        if (ts.isIdentifier(node)) {
          const symbol = checker.getSymbolAtLocation(node);
          if (symbol !== undefined && !(symbol.declarations ?? []).some((decl) => (decl as TS.NamedDeclaration).name === node)) {
            const target = symbolTarget(symbol);
            if ("key" in target) edge("references", node, target, "resolved_static", true);
          }
          return;
        }
        ts.forEachChild(node, visit);
      };
      ts.forEachChild(sf, visit);
    }
  }

  // ---------------------------------------------------------------- closure
  // A group exists only when its first member is a node this run emitted, and a group
  // only names the members that are. A resolved edge whose target no file contributed --
  // a file that did not parse, a declaration with no representable identity -- states
  // that instead of pointing at nothing.
  for (const group of [...groups.values()].sort((a, b) => compare(a.key, b.key))) {
    const members = group.members.filter((member) => nodes.has(member.key));
    if (!nodes.has(group.members[0]!.key) || members.length === 0) continue;
    nodes.set(group.key, { key: group.key, kind: group.kind, name: group.name, path: group.path, start: group.start, end: group.end });
    for (const member of members) {
      edges.push({ relationship: "groups", source: group.key, target: member.key, descriptor: null, status: "resolved", strength: "resolved_static", path: member.path, start: member.start, end: member.end });
    }
  }
  const closed = edges.map((item) =>
    item.status === "resolved" && !nodes.has(item.target!)
      ? { ...item, target: null, descriptor: bounded(`target-not-analysed:${item.target}`, MAX_DESCRIPTOR), status: "unresolved", strength: "syntactic" }
      : item,
  );
  if (nodes.size + closed.length > limits.max_candidates) throw new Refusal("snapshot produces more candidates than the profile admits");
  const edgeOrder = (a: EdgeOut, b: EdgeOut) =>
    compare(a.path, b.path) ||
    a.start - b.start ||
    a.end - b.end ||
    compare(a.relationship, b.relationship) ||
    compare(a.source, b.source) ||
    compare(a.target ?? "", b.target ?? "") ||
    compare(a.descriptor ?? "", b.descriptor ?? "");
  const reportOrder = (a: ReportOut, b: ReportOut) =>
    compare(a.path, b.path) || compare(a.outcome, b.outcome) || (a.start ?? -1) - (b.start ?? -1) || compare(a.detail, b.detail);
  return {
    format: RESPONSE_FORMAT,
    request_digest: request.request_digest,
    typescript_version: ts.version,
    programs,
    lib_files: [...libTexts.entries()].filter((pair) => pair[1] !== undefined).map((pair) => pair[0]).sort(compare),
    reports: reports.sort(reportOrder),
    nodes: [...nodes.values()].sort((a, b) => compare(a.key, b.key)),
    edges: closed.sort(edgeOrder),
  };
}

main();
