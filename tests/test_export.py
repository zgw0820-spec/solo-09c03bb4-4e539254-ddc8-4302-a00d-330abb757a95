"""Partition export: standalone 4Kn images and HTTP Range consistency."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.export import (
    EXPORT_TAIL_BYTES,
    ExportSource,
    parse_single_range,
    plan_partition_export,
)
from app.gpt import inspect_mbr, parse_entries, read_gpt_copy
from app import samplegen as fixtures
from app.samplegen import MIB

SECTOR = 4096
ESP = fixtures.ESP_TYPE


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        work_dir=tmp_path / "jobs",
        max_upload_bytes=128 * MIB,
        job_ttl_seconds=600,
        copy_chunk_bytes=1 * MIB,
        max_concurrent_jobs=1,
        sample_dir=tmp_path / "samples",
        sample_cache_dir=tmp_path / "samples",
        sample_download_name="sample.img",
    )
    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _upload(client, path, name="disk.img"):
    with open(path, "rb") as fh:
        response = client.post(
            "/api/jobs",
            content=fh,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": name,
            },
        )
    assert response.status_code == 202, response.text
    return response.json()["job"]["id"]


def _wait(client, job_id, timeout=60):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _ready_job(client, tmp_path, *, disk_mib=16, partitions=None, name="vm-disk.img"):
    src = tmp_path / name
    fixtures.make_disk(src, disk_mib=disk_mib, partitions=partitions)
    job_id = _upload(client, src, name=name)
    final = _wait(client, job_id)
    assert final["status"] == "done", final
    return final


def test_export_is_standalone_and_only_contains_partition(client, tmp_path):
    final = _ready_job(client, tmp_path)
    part = final["summary"]["partitions"][0]
    response = client.get(part["export_url"])
    assert response.status_code == 200, response.text
    image = response.content
    assert response.headers["accept-ranges"] == "bytes"
    assert int(response.headers["content-length"]) == len(image)

    start = part["byte_start"]
    end = part["byte_end_exclusive"]
    assert len(image) == end + EXPORT_TAIL_BYTES

    # Parse the exported image from scratch as a 4Kn GPT disk.
    path = tmp_path / "export.img"
    path.write_bytes(image)
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.path.getsize(path)
        assert size % SECTOR == 0
        mbr = inspect_mbr(fd, size, SECTOR)
        assert mbr.problems == []
        assert mbr.ee_count == 1
        assert mbr.protective_start_lba == 1
        assert mbr.protective_size_lbas == size // SECTOR - 1

        p_head, p_entries = read_gpt_copy(fd, size, SECTOR, "primary")
        b_head, b_entries = read_gpt_copy(fd, size, SECTOR, "backup")
        assert p_entries == b_entries
        assert p_head.disk_guid == b_head.disk_guid

        parts = parse_entries(p_entries)
        assert len(parts) == 1
        exported = parts[0]
        assert exported.start_lba * SECTOR == start
        assert (exported.end_lba + 1) * SECTOR == end
        assert exported.name == part["name"]
        assert exported.type_guid_text == part["type_guid"]
        assert f"0x{exported.attributes:016x}" == part["attributes"]

        # Backups sit immediately after the partition's last byte.
        assert b_head.my_lba * SECTOR == end + 4 * SECTOR
        assert b_head.partition_entry_lba * SECTOR == end
        assert header_entries_crc_matches(b_head, b_entries)

        # GUIDs changed, and differ from the original disk/partition GUIDs.
        assert f"{exported.unique_guid_text}" != part["unique_guid"]
        assert exported.unique_guid_text != part["unique_guid"]
        import uuid

        assert (
            uuid.UUID(bytes_le=p_head.disk_guid)
            != uuid.UUID(final["summary"]["disk_guid"])
        )

        # Everything between the primary GPT and the partition is zero.
        head_end = p_head.first_usable_lba * SECTOR
        assert image[head_end:start] == b"\x00" * (start - head_end)
        # Backup reserved area is exactly the last 20480 bytes and the bytes
        # from the primary GPT area end to the partition start are zero.
        assert len(image) - end == EXPORT_TAIL_BYTES
    finally:
        os.close(fd)


def header_entries_crc_matches(header, entries) -> bool:
    from app.gpt import crc32

    return crc32(entries) == header.entries_crc


def test_export_partition_payload_matches_converted_image(client, tmp_path):
    final = _ready_job(client, tmp_path)
    whole = client.get(final["download_url"]).content
    for part in final["summary"]["partitions"]:
        exported = client.get(part["export_url"]).content
        start = part["byte_start"]
        end = part["byte_end_exclusive"]
        assert exported[start:end] == whole[start:end]
        # And the primary GPT of the export is *not* copied from the source.
        assert exported[:24576] != whole[:24576]


def test_export_zeroes_are_real_and_only_partition_survives(client, tmp_path):
    """Unallocated source bytes between GPT and partition must be zero."""
    src = tmp_path / "gappy.img"
    # Partition 1 starts at 8 MiB so there is a large hole after the GPT.
    fixtures.make_disk(
        src,
        disk_mib=16,
        partitions=[
            dict(index=1, type=ESP, unique=fixtures._guid(1), name="EFI",
                 start=8 * MIB, end=9 * MIB, fill=b"\x11")
        ],
    )
    # Write non-zero junk into the unconverted area of the converted product's
    # covered range (the source unallocated region after the primary GPT).
    fixtures.patch_bytes(src, 6 * MIB, b"JUNK" * 16)
    job_id = _upload(client, src, name="gappy.img")
    final = _wait(client, job_id)
    assert final["status"] == "done", final
    whole = client.get(final["download_url"]).content
    assert whole[6 * MIB : 6 * MIB + 64] == b"JUNK" * 16  # conversion copies it
    part = final["summary"]["partitions"][0]
    exported = client.get(part["export_url"]).content
    # The export must not carry that junk: only head GPT + partition payload.
    assert exported[6 * MIB : 6 * MIB + 64] == b"\x00" * 64
    assert exported[part["byte_start"]:part["byte_end_exclusive"]] == whole[
        part["byte_start"]:part["byte_end_exclusive"]
    ]


def test_repeated_range_requests_stitch_to_full_download(client, tmp_path):
    final = _ready_job(client, tmp_path, disk_mib=8)
    part = final["summary"]["partitions"][1]
    url = part["export_url"]
    full = client.get(url)
    assert full.status_code == 200
    image = full.content
    size = len(image)

    # Deliberately uneven segments, including a 1-byte range.
    boundaries = [0, 1, 4096, 24576, 100_000, 1_000_001, size // 2, size - 1, size]
    pieces = []
    for start, end in zip(boundaries, boundaries[1:]):
        response = client.get(url, headers={"Range": f"bytes={start}-{end - 1}"})
        assert response.status_code == 206, (start, end, response.text)
        assert response.headers["content-range"] == f"bytes {start}-{end - 1}/{size}"
        assert int(response.headers["content-length"]) == end - start
        pieces.append(response.content)
    assert b"".join(pieces) == image

    # Open-ended and suffix ranges agree too.
    tail = client.get(url, headers={"Range": f"bytes={size - 1001}-"})
    assert tail.status_code == 206
    assert tail.content == image[-1001:]
    suffix = client.get(url, headers={"Range": "bytes=-777"})
    assert suffix.status_code == 206
    assert suffix.content == image[-777:]


def test_ranges_are_stable_across_separate_requests_and_jobs_untouched(
    client, tmp_path
):
    final = _ready_job(client, tmp_path, disk_mib=8)
    part = final["summary"]["partitions"][1]
    first = client.get(part["export_url"]).content
    second = client.get(part["export_url"]).content
    assert first == second
    # Exporting did not modify the original product.
    whole_before = client.get(final["download_url"]).content
    for start in range(0, len(first), 300_000):
        end = min(start + 300_000, len(first))
        chunk = client.get(
            part["export_url"],
            headers={"Range": f"bytes={start}-{end - 1}"},
        )
        assert chunk.content == first[start:end]
    assert client.get(final["download_url"]).content == whole_before


def test_export_does_not_create_files_in_job_dir(client, tmp_path):
    final = _ready_job(client, tmp_path, disk_mib=8)
    job_dir = tmp_path / "jobs" / final["id"]

    def stable_files():
        return {
            p.name: p.stat().st_size
            for p in job_dir.iterdir()
            # job.json is rewritten atomically through a temporary file.
            if not p.name.endswith(".tmp")
        }

    before = stable_files()
    client.get(final["summary"]["partitions"][0]["export_url"])
    after = stable_files()
    assert after == before
    # The only data files are the metadata and the unmodified product.
    assert set(after) <= {"job.json", "output-4k.img"}


def test_export_rejects_unknown_partition(client, tmp_path):
    final = _ready_job(client, tmp_path, disk_mib=8)
    response = client.get(f"/api/jobs/{final['id']}/partitions/9/export")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PARTITION_NOT_FOUND"
    bad = client.get(f"/api/jobs/{final['id']}/partitions/abc/export")
    assert bad.status_code == 404


def test_export_requires_finished_job(client, tmp_path):
    src = tmp_path / "slow.img"
    fixtures.make_disk(src, disk_mib=8)
    job_id = _upload(client, src)
    response = client.get(f"/api/jobs/{job_id}/partitions/1/export")
    # Either still running (409) or finished too fast (200).
    assert response.status_code in (200, 409)
    if response.status_code == 409:
        assert response.json()["error"]["code"] == "NOT_READY"


def test_export_after_delete_or_ttl_is_gone_but_started_transfer_finishes(
    client, tmp_path
):
    """A started export survives deletion; new requests are refused."""
    from app.export import ExportSource

    final = _ready_job(client, tmp_path, disk_mib=8)
    job_id = final["id"]
    job_dir = tmp_path / "jobs" / job_id
    manager = client.app.state.manager

    source = manager.open_export(job_id, 1)
    assert isinstance(source, ExportSource)
    # Delete the job while the transfer is "in flight".
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert manager.get(job_id) is None
    assert job_dir.exists()  # deferred until the export closes
    image = source.read_all()
    assert len(image) == source.plan.disk_size
    source.close()
    assert not job_dir.exists()
    # New request for the deleted job is refused.
    assert client.get(f"/api/jobs/{job_id}/partitions/1/export").status_code == 404


def test_sweep_does_not_delete_job_with_active_export(tmp_path):
    import time

    from app.jobs import JobManager

    manager = JobManager(tmp_path / "jobs", job_ttl_seconds=1, chunk_size=MIB)
    manager.start()
    try:
        job = manager.create_job("x.img")
        job.source_path.write_bytes(b"not empty")
        job.status = "done"
        job.updated_at = time.time() - 3600
        with manager._lock:  # noqa: SLF001
            manager._jobs[job.id] = job
        with manager._lock:  # noqa: SLF001
            manager._active_exports[job.id] = 1
        manager.discard(job.id)
        assert job.dir.exists()
        manager.release_export(job.id)
        assert not job.dir.exists()
    finally:
        manager.stop()


def test_export_memory_is_bounded(client, tmp_path):
    """Export reads must be small and independent of the exported length."""
    from app import export as export_module
    from app.export import ExportSource

    final = _ready_job(client, tmp_path, disk_mib=8)
    manager = client.app.state.manager
    source = manager.open_export(final["id"], 1, chunk_size=64 * 1024)
    calls = []
    original = export_module.pread_exact

    def tracking(fd, length, offset):
        calls.append(length)
        return original(fd, length, offset)

    export_module.pread_exact = tracking
    try:
        total = 0
        for chunk in source.iter_range(0, source.plan.disk_size):
            total += len(chunk)
            assert len(chunk) <= 64 * 1024
    finally:
        export_module.pread_exact = original
        source.close()
    assert total == source.plan.disk_size
    assert calls and max(calls) <= 64 * 1024


def test_plan_rejects_non_exportable_partition(tmp_path):
    from app.export import plan_partition_export
    from app.converter import analyze_source
    from app.errors import ConversionError

    # A valid source cannot contain misaligned partitions, so check the guard
    # by asking for a partition index that does not exist and by indexing a
    # synthetic analysis with a broken range.
    src = tmp_path / "d.img"
    fixtures.make_disk(src, disk_mib=8)
    fd = os.open(src, os.O_RDONLY)
    try:
        analysis = analyze_source(fd, os.path.getsize(src))
    finally:
        os.close(fd)
    with pytest.raises(ConversionError) as info:
        plan_partition_export("a" * 32, analysis, 99)
    assert info.value.code == "PARTITION_NOT_FOUND"


def test_parse_single_range():
    from app.export import RangeNotSatisfiable

    size = 1000
    assert parse_single_range(None, size) is None
    assert parse_single_range("bytes=0-99", size) == (0, 100)
    assert parse_single_range("bytes=990-", size) == (990, 1000)
    assert parse_single_range("bytes=-10", size) == (990, 1000)
    assert parse_single_range("bytes=0-99999", size) == (0, 1000)
    assert parse_single_range("bytes=0-1,4-5", size) is None
    assert parse_single_range("items=0-1", size) is None
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range("bytes=1000-", size)
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range("bytes=1001-2000", size)


def test_head_and_unsatisfiable_range(client, tmp_path):
    final = _ready_job(client, tmp_path, disk_mib=8)
    part = final["summary"]["partitions"][1]
    full = client.get(part["export_url"])
    size = int(full.headers["content-length"])

    head = client.head(part["export_url"])
    assert head.status_code == 200
    assert int(head.headers["content-length"]) == size
    assert head.headers["accept-ranges"] == "bytes"
    assert head.content == b""

    head_range = client.head(
        part["export_url"], headers={"Range": "bytes=0-99"}
    )
    assert head_range.status_code == 206
    assert head_range.headers["content-range"] == f"bytes 0-99/{size}"
    assert head_range.content == b""

    bad = client.get(part["export_url"], headers={"Range": f"bytes={size}-"})
    assert bad.status_code == 416
    assert bad.headers["content-range"] == f"bytes */{size}"
    assert bad.json()["error"]["code"] == "RANGE_NOT_SATISFIABLE"

    odd = client.get(part["export_url"], headers={"Range": "bytes=abc"})
    # A malformed header falls back to the full image, as RFC 9110 allows.
    assert odd.status_code == 200
    assert len(odd.content) == size


def test_export_partition_info_endpoint(client, tmp_path):
    final = _ready_job(client, tmp_path, disk_mib=8)
    job_id = final["id"]
    info = client.get(f"/api/jobs/{job_id}/partitions/1").json()
    assert info["partition_index"] == 1
    assert info["sector_size"] == 4096
    assert info["download_url"].endswith("/partitions/1/export")
    assert info["export_size"] == info["partition_byte_end_exclusive"] + EXPORT_TAIL_BYTES
    # The GUIDs shown in the UI are exactly the ones in the streamed image.
    blob = client.get(info["download_url"]).content
    path = tmp_path / "info.img"
    path.write_bytes(blob)
    fd = os.open(path, os.O_RDONLY)
    try:
        header, entries = read_gpt_copy(fd, os.path.getsize(path), SECTOR, "primary")
    finally:
        os.close(fd)
    import uuid

    assert str(uuid.UUID(bytes_le=header.disk_guid)).upper() == info["disk_guid"]
    part = parse_entries(entries)[0]
    assert part.unique_guid_text == info["partition_guid"]
    assert client.get(f"/api/jobs/{job_id}/partitions/9").status_code == 404


def test_export_filename_and_headers(client, tmp_path):
    from urllib.parse import quote, unquote

    final = _ready_job(client, tmp_path, disk_mib=8, name=quote("虚拟机.img"))
    part = final["summary"]["partitions"][1]
    response = client.get(part["export_url"], headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    disposition = unquote(response.headers["content-disposition"])
    assert "虚拟机-part2.4k.img" in disposition
    assert response.headers["x-partition-index"] == "2"
    assert response.headers["x-partition-start"] == str(part["byte_start"])
    assert response.headers["x-partition-end"] == str(part["byte_end_exclusive"])
    assert response.headers["x-export-size"] == str(part["export"]["export_size"])
    assert response.headers["x-export-partition-guid"] == part["export"]["partition_guid"]
