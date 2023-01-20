# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
"""
Test that SubdirData is able to use (or skip) incremental jlap downloads.
"""
import json
from pathlib import Path
import pytest

import requests
import zstandard

from conda.base.context import conda_tests_ctxt_mgmt_def_pol, context
from conda.common.io import env_vars
from conda.gateways.connection.session import CondaSession
from conda.gateways.repodata import CondaRepoInterface, RepoInterface, jlapper, repo_jlap

from conda.gateways.connection.session import CondaSession
from conda.models.channel import Channel
from conda.core.subdir_data import SubdirData


def test_server_available(package_server):
    port = package_server.getsockname()[1]
    response = requests.get(f"http://127.0.0.1:{port}/notfound")
    assert response.status_code == 404


def test_jlap_fetch(package_server, tmp_path, mocker):
    host, port = package_server.getsockname()
    base = f"http://{host}:{port}/test"

    url = f"{base}/osx-64"
    repo = repo_jlap.JlapRepoInterface(
        url,
        repodata_fn="repodata.json",
        cache_path_json=Path(tmp_path, "repodata.json"),
        cache_path_state=Path(tmp_path, "repodata.state.json"),
    )

    patched = mocker.patch(
        "conda.gateways.repodata.jlapper.download_and_hash", wraps=jlapper.download_and_hash
    )

    state = {}
    data_json = repo.repodata(state)

    assert patched.call_count == 1

    # second will try to fetch (non-existent) .jlap, then fall back to .json
    data_json = repo.repodata(state)  # a 304?

    data_json = repo.repodata(state)

    # TODO more useful assertions
    assert patched.call_count == 3


def test_download_and_hash(package_server, tmp_path: Path, mocker, package_repository_base: Path):
    host, port = package_server.getsockname()
    base = f"http://{host}:{port}/test"

    url = base + "/notfound.json.zst"
    session = CondaSession()
    state = {}
    destination = tmp_path / "download_not_found"
    # 404 is raised as an exception
    try:
        jlapper.download_and_hash(jlapper.hash(), url, destination, session, state)
    except requests.HTTPError as e:
        assert e.response.status_code == 404
        assert not destination.exists()
    else:
        assert False, "must raise"

    destination = tmp_path / "repodata.json"
    url2 = base + "/osx-64/repodata.json"
    hasher2 = jlapper.hash()
    response = jlapper.download_and_hash(hasher2, url2, destination, session, state)
    print(response)
    print(state)
    t = destination.read_text()
    assert len(t)

    # assert we don't clobber if 304 not modified
    response2 = jlapper.download_and_hash(
        jlapper.hash(), url2, destination, session, {"_etag": response.headers["etag"]}
    )
    assert response2.status_code == 304
    assert destination.read_text() == t
    # however the hash will not be recomputed

    (package_repository_base / "osx-64" / "repodata.json.zst").write_bytes(
        zstandard.ZstdCompressor().compress(
            (package_repository_base / "osx-64" / "repodata.json").read_bytes()
        )
    )

    url3 = base + "/osx-64/repodata.json.zst"
    dest_zst = tmp_path / "repodata.json.from-zst"  # should be decompressed
    assert not dest_zst.exists()
    hasher3 = jlapper.hash()
    response3 = jlapper.download_and_hash_zst(hasher3, url3, dest_zst, session, {})
    assert response3.status_code == 200
    assert int(response3.headers["content-length"]) < dest_zst.stat().st_size

    assert destination.read_text() == dest_zst.read_text()

    # hashes the decompressed data
    assert hasher2.digest() == hasher3.digest()


@pytest.mark.parametrize("use_jlap", [True, False])
def test_repodata_state(
    package_server, tmp_path: Path, mocker, package_repository_base: Path, use_jlap
):
    """
    Test that .state.json file works correctly.
    """
    host, port = package_server.getsockname()
    base = f"http://{host}:{port}/test"
    channel_url = f"{base}/osx-64"

    if use_jlap:
        repo_cls = repo_jlap.JlapRepoInterface
    else:
        repo_cls = CondaRepoInterface

    with env_vars(
        {"CONDA_PLATFORM": "osx-64", "CONDA_EXPERIMENTAL": "jlap" if use_jlap else ""},
        stack_callback=conda_tests_ctxt_mgmt_def_pol,
    ):
        # XXX how does this not do anything?
        SubdirData.clear_cached_local_channel_data()  # should clear in-memory caches
        SubdirData._cache_.clear()  # definitely clears them, including normally-excluded file:// urls

        test_channel = Channel(channel_url)
        sd = SubdirData(channel=test_channel)

        # parameterize whether this is used?
        assert isinstance(sd._repo, repo_cls)

        print(sd.repodata_fn)

        assert sd._loaded is False

        # shoud automatically fetch and load
        assert len(list(sd.iter_records()))

        assert sd._loaded is True

        # now let's check out state file
        state = json.loads(Path(sd.cache_path_state).read_text())

        # not all required depending on server response, but our test server
        # will include them
        for field in ("mod", "etag", "cache_control", "size", "mtime_ns"):
            assert field in state
            assert not f"_{field}" in state


@pytest.mark.parametrize("use_jlap", ["jlap", "jlapopotamus", "jlap,another", ""])
def test_jlap_flag(use_jlap):
    """
    Test that CONDA_EXPERIMENTAL is a comma-delimited list.
    """

    with env_vars(
        {"CONDA_EXPERIMENTAL": use_jlap},
        stack_callback=conda_tests_ctxt_mgmt_def_pol,
    ):
        expected = "jlap" in use_jlap.split(",")
        assert ("jlap" in context.experimental) is expected
