from syncade.process import SubprocessResult, SubprocessTimeoutError
from tests.persistence._helpers import _make_round_dir


class TestPersistProducerResult:
    """PR-8: ``persist_producer_result`` writes
    ``producer.{stdout,stderr,commit.txt[,error.txt]}`` to the
    round directory. Mirrors the synthesizer + test-run
    persistence layout."""

    def _committed_result(self):
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=12.3,
            output=ProducerOutput(narrative_text="I fixed the null pointer in foo.py."),
            error=None,
            raw_subprocess_result=SubprocessResult(
                returncode=0,
                stdout="(claude envelope JSON)",
                stderr="",
                duration_seconds=12.3,
            ),
        )

    def _stalled_result(self):
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="stalled",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=8.0,
            output=ProducerOutput(narrative_text="I cannot fix this without more info."),
            error=None,
            raw_subprocess_result=SubprocessResult(
                returncode=0,
                stdout="(claude envelope JSON)",
                stderr="",
                duration_seconds=8.0,
            ),
        )

    def _subprocess_error_result(self):
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="subprocess_error",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=5.0,
            output=None,
            error=SubprocessTimeoutError(
                "claude timed out", stdout="partial", stderr="err", timeout=10.0
            ),
            raw_subprocess_result=SubprocessResult(
                returncode=-1,
                stdout="partial",
                stderr="err",
                duration_seconds=5.0,
            ),
        )

    def _indeterminate_subprocess_error_result(self):
        from syncade.producer import ProducerResult

        return ProducerResult(
            outcome="subprocess_error",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=5.0,
            output=None,
            error=SubprocessTimeoutError(
                "claude timed out", stdout="partial", stderr="err", timeout=10.0
            ),
            raw_subprocess_result=SubprocessResult(
                returncode=-1,
                stdout="partial",
                stderr="err",
                duration_seconds=5.0,
            ),
        )

    def test_committed_writes_all_files(self, tmp_path):
        from syncade.persistence import persist_producer_result

        round_dir = _make_round_dir(tmp_path)
        paths = persist_producer_result(round_dir, self._committed_result())

        assert paths.stdout.read_text() == "I fixed the null pointer in foo.py."
        assert paths.stderr.read_text() == ""
        assert paths.commit_sha.read_text().strip() == "b" * 40
        assert paths.error is None
        assert not (round_dir / "producer.error.txt").exists()

    def test_stalled_writes_starting_sha_as_commit(self, tmp_path):
        from syncade.persistence import persist_producer_result

        round_dir = _make_round_dir(tmp_path)
        paths = persist_producer_result(round_dir, self._stalled_result())
        # Stalled: ending == starting, so commit.txt is the starting SHA
        assert paths.commit_sha.read_text().strip() == "a" * 40
        # Narrative still preserved
        assert "cannot fix" in paths.stdout.read_text()

    def test_subprocess_error_writes_error_txt(self, tmp_path):
        from syncade.persistence import persist_producer_result

        round_dir = _make_round_dir(tmp_path)
        paths = persist_producer_result(round_dir, self._subprocess_error_result())
        assert paths.error is not None
        error_text = paths.error.read_text()
        assert "SubprocessTimeoutError" in error_text
        assert "timed out" in error_text
        # commit.txt records ending_sha == starting_sha
        assert paths.commit_sha.read_text().strip() == "a" * 40
        # stdout falls back to raw_subprocess_result.stdout
        assert "partial" in paths.stdout.read_text()

    def test_subprocess_error_with_moved_head_surfaces_indeterminate_commit(self, tmp_path):
        from syncade.persistence import persist_producer_result

        round_dir = _make_round_dir(tmp_path)
        paths = persist_producer_result(round_dir, self._indeterminate_subprocess_error_result())

        assert paths.commit_sha.read_text() == f"{'b' * 40}\n"
        assert paths.error is not None
        error_text = paths.error.read_text()
        assert "Indeterminate producer commit" in error_text
        assert "a" * 40 in error_text
        assert "b" * 40 in error_text
