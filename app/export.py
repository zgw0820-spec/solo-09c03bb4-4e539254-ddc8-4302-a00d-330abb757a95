"""Stream a single partition as a standalone 4 KiB-sector GPT image.

Nothing is written to disk: the exported image is generated while it is sent
to the client.  The byte stream is fully deterministic — the replacement disk
and partition GUIDs are derived from stable inputs — so independent HTTP Range
requests against the same download URL can be concatenated into exactly the
bytes of a full download.

Exported layout (all offsets in bytes, 4096-byte logical sectors)::

    0 .. 24576          protective MBR + primary GPT (4 entry sectors)
    24576 .. start      zeros
    start .. end        selected partition payload, byte-for-byte from the
                        converted image (same byte position as in the source)
    end .. end + 20480  backup GPT: 4 entry sectors immediately followed by
                        the backup header

Everything outside those areas is zero.  Memory use is bounded by the read
chunk size; only the fixed-size GPT areas are held in memory.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator
from urllib.parse import quote

from starlette.responses import StreamingResponse

from .errors import ConversionError
from .gpt import (
    COPY_CHUNK,
    GPTLayout,
    ZERO_GUID,
    build_layout,
    entry_sectors,
    pread_exact,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .converter import SourceAnalysis

EXPORT_SECTOR_SIZE = 4096
# 4 entry sectors + 1 header sector.  The backup GPT starts exactly at the
# partition's end so that "the backup GPT immediately follows the partition".
EXPORT_TAIL_SECTORS = entry_sectors(EXPORT_SECTOR_SIZE) + 1
EXPORT_TAIL_BYTES = EXPORT_TAIL_SECTORS * EXPORT_SECTOR_SIZE  # 20480

EXPORT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "urn:gpt-512e-to-4kn:partition-export:v1",
)


class RangeNotSatisfiable(Exception):
    """The requested byte range starts beyond the exported image."""

    def __init__(self, size: int) -> None:
        super().__init__("range not satisfiable")
        self.size = size


def _derive_guid(
    kind: str,
    job_id: str,
    partition_index: int,
    old_disk_guid: bytes,
    old_entry_guid: bytes,
    avoid: set[bytes],
) -> uuid.UUID:
    """Derive a stable replacement GUID that differs from all ``avoid`` values."""
    seed = (
        f"{kind}|{job_id}|{partition_index}|"
        f"{old_disk_guid.hex()}|{old_entry_guid.hex()}"
    )
    counter = 0
    while True:
        candidate = uuid.uuid5(
            EXPORT_NAMESPACE, seed if counter == 0 else f"{seed}|{counter}"
        )
        if candidate.bytes_le not in avoid and candidate.bytes_le != ZERO_GUID:
            return candidate
        counter += 1


@dataclass(frozen=True)
class ExportPlan:
    job_id: str
    partition_index: int
    disk_size: int
    partition_start: int
    partition_end: int
    source_offset: int
    disk_guid: uuid.UUID
    partition_guid: uuid.UUID
    head: bytes
    tail: bytes
    layout: GPTLayout

    @property
    def disk_guid_text(self) -> str:
        return str(self.disk_guid).upper()

    @property
    def partition_guid_text(self) -> str:
        return str(self.partition_guid).upper()

    def metadata(self) -> dict:
        return {
            "partition_index": self.partition_index,
            "sector_size": EXPORT_SECTOR_SIZE,
            "export_size": self.disk_size,
            "partition_byte_start": self.partition_start,
            "partition_byte_end_exclusive": self.partition_end,
            "disk_guid": self.disk_guid_text,
            "partition_guid": self.partition_guid_text,
        }

    def metadata_headers(self) -> dict[str, str]:
        return {
            "X-Partition-Index": str(self.partition_index),
            "X-Partition-Start": str(self.partition_start),
            "X-Partition-End": str(self.partition_end),
            "X-Export-Size": str(self.disk_size),
            "X-Export-Disk-Guid": self.disk_guid_text,
            "X-Export-Partition-Guid": self.partition_guid_text,
        }


def plan_partition_export(
    job_id: str, analysis: "SourceAnalysis", partition_index: int
) -> ExportPlan:
    """Build the metadata/GPT for a standalone image of one partition."""
    partition = next(
        (p for p in analysis.partitions if p.index == partition_index), None
    )
    if partition is None:
        raise ConversionError(
            "PARTITION_NOT_FOUND",
            f"源盘没有编号为 {partition_index} 的分区。",
            details={
                "partitions": [p.index for p in analysis.partitions],
            },
            status_code=404,
        )
    if partition.start_lba > partition.end_lba:
        raise ConversionError(
            "EXPORT_PARTITION_INVALID",
            f"分区 {partition_index} 的范围无效，无法导出。",
            status_code=409,
        )

    source_sector = analysis.sector_size
    start = partition.start_lba * source_sector
    end = (partition.end_lba + 1) * source_sector
    if start % EXPORT_SECTOR_SIZE or end % EXPORT_SECTOR_SIZE:
        raise ConversionError(
            "EXPORT_PARTITION_NOT_ALIGNED",
            f"分区 {partition_index} 的字节边界未对齐到 "
            f"{EXPORT_SECTOR_SIZE} 字节，无法导出为 4Kn 镜像。",
            status_code=409,
        )

    start_lba = start // EXPORT_SECTOR_SIZE
    end_lba = end // EXPORT_SECTOR_SIZE - 1
    old_disk = analysis.header.disk_guid
    old_unique = partition.unique_guid
    if old_unique == ZERO_GUID:
        raise ConversionError(
            "EXPORT_PARTITION_INVALID",
            f"分区 {partition_index} 没有唯一标识 GUID，无法导出。",
            status_code=409,
        )

    disk_guid = _derive_guid(
        "disk", job_id, partition_index, old_disk, old_unique, {old_disk, old_unique}
    )
    partition_guid = _derive_guid(
        "part",
        job_id,
        partition_index,
        old_disk,
        old_unique,
        {old_disk, old_unique, disk_guid.bytes_le},
    )

    # Only the unique GUID changes; type GUID, attributes and the raw UTF-16LE
    # name bytes are carried over verbatim.  build_layout() then rewrites the
    # LBA fields for 4 KiB sectors.  The original table index is kept so boot
    # entries that refer to "partition N" still hit the same partition.
    raw = bytearray(partition.raw)
    raw[16:32] = partition_guid.bytes_le

    disk_size = end + EXPORT_TAIL_BYTES
    try:
        layout = build_layout(
            disk_size,
            EXPORT_SECTOR_SIZE,
            disk_guid.bytes_le,
            [(partition.index, bytes(raw), start_lba, end_lba)],
        )
    except ValueError as exc:
        raise ConversionError(
            "EXPORT_LAYOUT_FAILED",
            f"无法为分区 {partition_index} 生成独立 GPT：{exc}",
            status_code=409,
        ) from exc

    head, tail = layout.expected_reserved_bytes()
    if start < len(head):
        # A converted image can never reach this state (the converter rejects
        # partitions that overlap the new primary GPT area), but never emit a
        # silently corrupt image if it somehow does.
        raise ConversionError(
            "EXPORT_PARTITION_CONFLICT",
            f"分区 {partition_index} 与新建主 GPT 保留区重叠，无法导出。",
            status_code=409,
        )
    if len(tail) != EXPORT_TAIL_BYTES:
        raise RuntimeError("export tail size mismatch")

    return ExportPlan(
        job_id=job_id,
        partition_index=partition_index,
        disk_size=disk_size,
        partition_start=start,
        partition_end=end,
        source_offset=start,
        disk_guid=disk_guid,
        partition_guid=partition_guid,
        head=head,
        tail=tail,
        layout=layout,
    )


def _place(dst: bytearray, dst_start: int, src: bytes, src_start: int) -> None:
    """Copy the overlap of ``src[src_start:]`` into ``dst`` at ``dst_start``."""
    lo = max(dst_start, src_start)
    hi = min(dst_start + len(dst), src_start + len(src))
    if lo < hi:
        dst[lo - dst_start : hi - dst_start] = src[lo - src_start : hi - src_start]


class ExportSource:
    """An open original output image plus its export plan.

    The file descriptor is pinned when the export starts, so removing the job
    (manual delete or TTL reaper) does not interrupt an export in progress.
    """

    def __init__(
        self,
        plan: ExportPlan,
        fd: int,
        *,
        filename: str,
        chunk_size: int = COPY_CHUNK,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.plan = plan
        self.filename = filename
        self.chunk_size = max(1, chunk_size)
        self._fd: int | None = fd
        self._on_close = on_close

    @property
    def closed(self) -> bool:
        return self._fd is None

    def read(self, offset: int, length: int) -> bytes:
        """Return at most ``length`` bytes of the exported image at ``offset``."""
        fd = self._fd
        if fd is None:
            raise ValueError("export source is closed")
        plan = self.plan
        if offset < 0 or length < 0 or offset + length > plan.disk_size:
            raise ValueError("read outside the exported image")
        if length == 0:
            return b""

        buf = bytearray(length)
        end = offset + length

        # Protective MBR + primary GPT.
        _place(buf, offset, plan.head, 0)

        # Partition payload: same byte position, copied from the converted
        # image with a bounded pread.
        lo = max(offset, plan.partition_start)
        hi = min(end, plan.partition_end)
        if lo < hi:
            src_offset = plan.source_offset + (lo - plan.partition_start)
            buf[lo - offset : hi - offset] = pread_exact(fd, hi - lo, src_offset)

        # Backup GPT begins exactly at the partition end.
        _place(buf, offset, plan.tail, plan.partition_end)
        # Every byte not covered above stays zero (bytearray initialisation).

        return bytes(buf)

    def iter_range(self, start: int, end: int) -> Iterator[bytes]:
        """Yield ``[start, end)`` in bounded chunks."""
        if start < 0 or end > self.plan.disk_size or start > end:
            raise ValueError("range outside the exported image")
        offset = start
        while offset < end:
            want = min(self.chunk_size, end - offset)
            data = self.read(offset, want)
            if not data:
                # A truncated source would otherwise loop forever.
                raise EOFError(f"unexpected EOF at byte {offset}")
            yield data
            offset += len(data)

    def read_all(self) -> bytes:
        """Full image as one bytes object (tests/diagnostics only)."""
        return b"".join(self.iter_range(0, self.plan.disk_size))

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)
        hook, self._on_close = self._on_close, None
        if hook is not None:
            hook()

    def __enter__(self) -> "ExportSource":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - safety net
        try:
            self.close()
        except Exception:
            pass


def parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single byte range (``Range: bytes=...``).

    Returns ``(start, end)`` with an exclusive end, or ``None`` when the
    header is absent/malformed/multi-range (in which case the caller sends the
    whole image, as RFC 9110 allows).  Raises :class:`RangeNotSatisfiable`
    when the requested start lies beyond the end of the image.
    """
    if not value:
        return None
    header = value.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[len("bytes=") :].strip()
    if not spec or "," in spec or "-" not in spec:
        # Multi-range requests are deliberately not implemented: the contract
        # only covers repeated single-segment requests.
        return None
    first, last = spec.split("-", 1)
    first, last = first.strip(), last.strip()
    try:
        if not first:
            if not last:
                return None
            suffix = int(last)
            if suffix <= 0:
                return None
            return max(size - suffix, 0), size
        start = int(first)
        if start < 0:
            return None
        if last:
            end = int(last) + 1
            if end <= start:
                # start > last is a malformed range, not an unsatisfiable one.
                return None
        else:
            end = size
    except ValueError:
        return None
    if start >= size:
        raise RangeNotSatisfiable(size)
    return start, min(end, size)


def _content_disposition(filename: str) -> str:
    try:
        filename.encode("ascii")
        return f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        return (
            "attachment; filename=\"partition.img\"; "
            f"filename*=utf-8''{quote(filename, safe='')}"
        )


class ExportResponse(StreamingResponse):
    """Stream one partition export, honouring a single byte range.

    Content-Length (and, for ranges, Content-Range) is always set, so clients
    can stitch several range requests back into the full image.
    """

    def __init__(
        self,
        source: ExportSource,
        *,
        range_header: str | None,
        filename: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        size = source.plan.disk_size
        try:
            byte_range = parse_single_range(range_header, size)
        except RangeNotSatisfiable as exc:
            source.close()
            raise
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": _content_disposition(filename),
            "X-Content-Type-Options": "nosniff",
        }
        if extra_headers:
            headers.update(extra_headers)
        if byte_range is None:
            start, end = 0, size
            status = 200
        else:
            start, end = byte_range
            headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
            status = 206
        headers["Content-Length"] = str(end - start)
        super().__init__(
            source.iter_range(start, end),
            status_code=status,
            headers=headers,
            media_type="application/octet-stream",
        )
        self._source = source

    async def __call__(self, scope, receive, send) -> None:
        # Starlette routes GET to HEAD as well and StreamingResponse would
        # otherwise stream the whole image.  Answer HEAD with headers only.
        if scope.get("type") == "http" and scope["method"].upper() == "HEAD":
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": self.status_code,
                        "headers": self.raw_headers,
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )
            finally:
                self._source.close()
            return
        await super().__call__(scope, receive, send)

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # Async generators are not closed by Starlette; close explicitly so
            # the pinned source file descriptor is always released.
            self._source.close()
            body = self.body_iterator
            aclose = getattr(body, "aclose", None)
            if aclose is not None:
                await aclose()
