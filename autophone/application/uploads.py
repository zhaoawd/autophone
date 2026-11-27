"""Bounded staging for HTTP-uploaded Assets."""
from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any


class UploadApplicationError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class UploadApplication:
    def __init__(
        self,
        *,
        package_store: Any,
        max_ipa_bytes: int,
        max_case_xlsx_bytes: int,
        case_stage_root: Path | None = None,
    ) -> None:
        self.package_store = package_store
        self.max_ipa_bytes = max_ipa_bytes
        self.max_case_xlsx_bytes = max_case_xlsx_bytes
        self.case_stage_root = case_stage_root

    async def stage_ipa(
        self,
        filename: str,
        chunks: AsyncIterable[bytes],
        *,
        content_length: int | None,
    ) -> tuple[Path, int]:
        if content_length is not None and content_length > self.max_ipa_bytes:
            raise UploadApplicationError(
                413,
                f"IPA upload too large: {content_length} bytes > {self.max_ipa_bytes}",
            )
        try:
            destination = self.package_store.staged_upload_path(filename)
        except self.package_store.PackageError as error:
            raise UploadApplicationError(422, str(error)) from error
        return await self._write_bounded(
            destination,
            chunks,
            maximum=self.max_ipa_bytes,
            label="IPA",
        )

    async def stage_case_xlsx(
        self,
        app_name: str,
        chunks: AsyncIterable[bytes],
        *,
        content_length: int | None,
        stage_directory: Path | None = None,
    ) -> Path:
        if content_length is not None and content_length > self.max_case_xlsx_bytes:
            raise UploadApplicationError(
                413,
                f"xlsx upload too large: > {self.max_case_xlsx_bytes} bytes",
            )
        descriptor, staged_name = tempfile.mkstemp(
            dir=stage_directory or self.case_app_stage_dir(app_name),
            prefix="upload-",
            suffix=".xlsx",
        )
        destination = Path(staged_name)
        try:
            total = 0
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_case_xlsx_bytes:
                        raise UploadApplicationError(
                            413,
                            f"xlsx upload too large: > {self.max_case_xlsx_bytes} bytes",
                        )
                    stream.write(chunk)
            if total == 0:
                raise UploadApplicationError(422, "uploaded file is empty")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def case_stage_dir(self) -> Path:
        root = self.case_stage_root or Path(tempfile.gettempdir()) / "autophone-case-imports"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        return root

    def case_app_stage_dir(self, app_name: str) -> Path:
        root = self.case_stage_dir().resolve()
        app_directory = (root / app_name).resolve()
        if app_directory.parent != root:
            raise UploadApplicationError(
                422,
                f"invalid app name for case import staging: {app_name!r}",
            )
        app_directory.mkdir(parents=False, exist_ok=True, mode=0o700)
        app_directory.chmod(0o700)
        return app_directory

    @staticmethod
    async def _write_bounded(
        destination: Path,
        chunks: AsyncIterable[bytes],
        *,
        maximum: int,
        label: str,
    ) -> tuple[Path, int]:
        total = 0
        try:
            with destination.open("wb") as stream:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > maximum:
                        raise UploadApplicationError(
                            413,
                            f"{label} upload too large: > {maximum} bytes",
                        )
                    stream.write(chunk)
            if total == 0:
                raise UploadApplicationError(422, "uploaded file is empty")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination, total
