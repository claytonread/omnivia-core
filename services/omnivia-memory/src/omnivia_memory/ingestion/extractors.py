"""Content extractors for supported file formats."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from omnivia_memory.ingestion.models import ExtractionResult


class BaseExtractor(ABC):
    """Abstract base class for content extractors."""

    @abstractmethod
    def extract(self, file_path: Path) -> ExtractionResult:
        """Extract text content from a file."""
        pass


class MarkdownExtractor(BaseExtractor):
    """Extracts text from Markdown files."""

    def extract(self, file_path: Path) -> ExtractionResult:
        """Read markdown file as plain text."""
        try:
            content = file_path.read_text(encoding="utf-8")
            return ExtractionResult.success(content)
        except Exception as e:
            return ExtractionResult.failure(f"Failed to read Markdown: {e}")


class TextExtractor(BaseExtractor):
    """Extracts text from plain text files."""

    def extract(self, file_path: Path) -> ExtractionResult:
        """Read a text file as UTF-8 plain text."""
        try:
            content = file_path.read_text(encoding="utf-8")
            return ExtractionResult.success(content)
        except Exception as e:
            return ExtractionResult.failure(f"Failed to read text: {e}")


class PDFExtractor(BaseExtractor):
    """Extracts text from PDF files."""

    def extract(self, file_path: Path) -> ExtractionResult:
        """Extract text from PDF using PyMuPDF."""
        try:
            import fitz  # PyMuPDF

            doc = fitz.open(str(file_path))
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text())
            doc.close()
            content = "\n".join(text_parts)
            return ExtractionResult.success(content)
        except ImportError:
            return ExtractionResult.failure("PyMuPDF not installed: pip install pymupdf")
        except Exception as e:
            return ExtractionResult.failure(f"Failed to extract PDF: {e}")


class DOCXExtractor(BaseExtractor):
    """Extracts text from DOCX files."""

    def extract(self, file_path: Path) -> ExtractionResult:
        """Extract text from DOCX using python-docx."""
        try:
            from docx import Document

            doc = Document(str(file_path))
            paragraphs = []
            for para in doc.paragraphs:
                if para.text.strip():
                    paragraphs.append(para.text.strip())
            content = "\n".join(paragraphs)
            return ExtractionResult.success(content)
        except ImportError:
            return ExtractionResult.failure("python-docx not installed: pip install python-docx")
        except Exception as e:
            return ExtractionResult.failure(f"Failed to extract DOCX: {e}")
