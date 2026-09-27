import json
from pathlib import Path
from typing import Any

import pytest

from data_pipeline.classify import TaskCategory, classify_example, classify_jsonl
from data_pipeline.common import (
    Checkpoint,
    SourceContext,
    iter_input_records,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError
from data_pipeline.deduplicate import deduplicate_jsonl
from data_pipeline.download import ResumableDownloader, download_sources
from data_pipeline.filter import filter_example, filter_jsonl, quality_score
from data_pipeline.inspect import inspect_source
from data_pipeline.mixture import create_mixture
from data_pipeline.normalize import normalize_jsonl, normalize_record
from data_pipeline.prepare import prepare_sources
from data_pipeline.split import split_jsonl
from data_pipeline.statistics import calculate_statistics
from data_pipeline.validate import validate_jsonl, validate_record


def write_jsonl(path: Path, records: list[Any]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def normalized_example(identifier: str, task_type: str = "TOOL_USE") -> dict[str, Any]:
    return {
        "id": identifier,
        "source": "test",
        "task_type": task_type,
        "messages": [
            {"role": "user", "content": f"goal {identifier}"},
            {
                "role": "assistant",
                "content": "calling a safe tool",
                "tool_calls": [{"name": "lookup", "arguments": {"q": identifier}}],
            },
            {"role": "tool", "content": "result"},
        ],
        "tool_calls": [{"name": "lookup", "arguments": {"q": identifier}}],
        "tool_results": [{"role": "tool", "content": "result"}],
        "metadata": {},
        "license": "test-license",
        "quality_score": 1.0,
    }


def test_pipeline_config_loads_sources_and_mixture() -> None:
    config = PipelineConfig.load(Path("configs/dataset.toml"))

    assert set(config.sources) >= {
        "starcoder",
        "toprak",
        "nemotron",
        "neulab",
    }
    assert config.mixture["tool_use"] > 0
    # Raised from 10_000 on 2026-09-25 for the agent-weighted acquisition run.
    assert config.limits["max_records"] == 6_000
    # neulab_swe was enabled on 2026-09-25 (real SWE-agent trajectories).
    assert config.source("neulab_swe").enabled is True
    # The agent-weighted sources added 2026-09-25 must all stay enabled.
    for name in (
        "toolace",
        "agentforge",
        "hermes_fc",
        "simple_agent",
        "self_oss",
        "magicoder",
        "nemotron_ia",
        "smoltalk_magpie",
        "smoltalk_core",
        "starcoder_shards",
        "neulab_browser",
        "neulab_code_feedback",
    ):
        assert config.source(name).enabled is True, name


def test_validation_reports_malformed_and_preserves_continuation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.jsonl"
    source.write_text(
        '{"messages":[{"role":"user","content":"hello"}]}\nnot-json\n{"messages":[]}\n',
        encoding="utf-8",
    )
    report = validate_jsonl(
        source,
        source="Toprak1yu/agent-tool-use-trajectories",
        source_format="jsonl",
        report_path=tmp_path / "report.json",
        rejects_path=tmp_path / "rejects.jsonl",
    )

    assert report.counters["records_seen"] == 3
    assert report.counters["accepted"] == 1
    assert report.counters["malformed"] == 1
    assert report.counters["rejected"] == 1
    assert (
        len((tmp_path / "rejects.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    )


def test_validation_accepts_serialized_messages_and_starcoder_messages() -> None:
    serialized = [
        json.dumps({"role": "user", "content": "Run a tool"}),
        json.dumps(
            {"role": "assistant", "content": "", "tool_calls": [{"name": "run"}]}
        ),
    ]

    assert validate_record({"messages": serialized}, "Toprak1yu/data") is None
    assert (
        validate_record(
            {"id": "1", "messages": [{"role": "user", "content": "Write code"}]},
            "OLMo-Coding/starcoder-python-instruct",
        )
        is None
    )


def test_normalization_decodes_serialized_messages() -> None:
    context = SourceContext("Toprak1yu/data", "apache-2.0")
    normalized, reason = normalize_record(
        {
            "messages": [
                json.dumps({"role": "user", "content": "Run a tool"}),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"name": "run", "arguments": {}}],
                    }
                ),
            ]
        },
        context,
        1,
    )

    assert reason is None
    assert normalized is not None
    assert normalized["messages"][1]["tool_calls"][0]["name"] == "run"


def test_normalization_preserves_provenance_and_tool_calls(tmp_path: Path) -> None:
    context = SourceContext(
        source="Toprak1yu/agent-tool-use-trajectories",
        license_name="apache-2.0",
        revision="rev-1",
        file_name="data/train.parquet",
        format="parquet",
    )
    record = {
        "messages": [
            {"role": "system", "content": "Use tools"},
            {"role": "user", "content": "Find a record"},
            {
                "role": "assistant",
                "content": "Calling",
                "tool_calls": [
                    {"function": {"name": "query", "arguments": '{"id": 1}'}}
                ],
            },
            {"role": "tool", "content": '{"value": 1}'},
        ],
        "license": "apache-2.0",
        "tools": [{"name": "query"}],
    }

    normalized, reason = normalize_record(record, context, 4)

    assert reason is None
    assert normalized is not None
    assert normalized["source"] == context.source
    assert normalized["messages"][2]["tool_calls"][0] == {
        "name": "query",
        "arguments": {"id": 1},
    }
    assert normalized["tool_results"][0]["role"] == "tool"
    assert normalized["metadata"]["row_number"] == 4
    assert normalized["metadata"]["license"] == "apache-2.0"
    assert normalized["metadata"]["tools"] == [{"name": "query"}]
    assert normalized["metadata"]["agentic_flow"]["tool_selection_count"] == 1
    assert (
        normalized["metadata"]["agentic_flow"]["permission_check"]
        == "not_present_in_source"
    )


def test_normalization_supports_neulab_action_trajectories() -> None:
    record = {
        "id": "swe-1",
        "content": [
            {
                "class_": "text_observation",
                "content": "Fix the failing test",
                "source": "user",
            },
            {
                "class_": "api_action",
                "function": "run_tests",
                "kwargs": {"suite": "unit"},
                "description": "Run tests",
            },
            {
                "class_": "text_observation",
                "content": "Traceback",
                "source": "environment",
            },
        ],
        "details": {"environment": "swe"},
    }
    context = SourceContext(
        "neulab/agent-data-collection", "MIT", file_name="swe/full_std.jsonl"
    )

    normalized, reason = normalize_record(record, context, 1)

    assert reason is None
    assert normalized is not None
    assert normalized["messages"][0]["role"] == "user"
    assert normalized["tool_calls"][0]["name"] == "run_tests"
    assert normalized["tool_results"][0]["content"] == "Traceback"


def test_checkpoint_and_json_replace_existing_windows_targets(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path / "stage.checkpoint.json")
    checkpoint.state = {"last_line": 1}
    checkpoint.save()
    checkpoint.state = {"last_line": 2}
    checkpoint.save()
    write_json(tmp_path / "report.json", {"first": True})
    write_json(tmp_path / "report.json", {"second": True})

    assert (
        json.loads((tmp_path / "stage.checkpoint.json").read_text())["last_line"] == 2
    )
    assert json.loads((tmp_path / "report.json").read_text()) == {"second": True}


def test_normalization_is_resumable(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    write_jsonl(
        source,
        [
            {"id": "1", "instruction": "one", "text": "1"},
            {"id": "2", "instruction": "two", "text": "2"},
        ],
    )
    output = tmp_path / "normalized.jsonl"
    report = tmp_path / "report.json"
    rejects = tmp_path / "rejects.jsonl"
    context = SourceContext("starcoder", "apache-2.0", file_name=str(source))

    first = normalize_jsonl(
        source,
        output,
        context=context,
        report_path=report,
        rejects_path=rejects,
        max_records=1,
    )
    second = normalize_jsonl(
        source,
        output,
        context=context,
        report_path=report,
        rejects_path=rejects,
    )

    assert first.counters["accepted"] == 1
    assert second.counters["accepted"] == 1
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_classification_is_evidence_based_and_preserves_unclassified() -> None:
    code_label, code_evidence = classify_example(
        {"messages": [{"role": "user", "content": "Write Python code"}]},
        source="starcoder",
    )
    unknown_label, unknown_evidence = classify_example(
        {"messages": [{"role": "user", "content": "A short statement"}]},
        source="unknown",
    )

    assert code_label == TaskCategory.CODING.value
    assert code_evidence
    assert unknown_label is None
    assert unknown_evidence == []


def test_classifier_does_not_label_source_subset_without_evidence() -> None:
    label, evidence = classify_example(
        {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
        },
        source="neulab_openhands",
        subset="openhands",
    )

    assert label is None
    assert evidence == ["software_source_without_content_evidence"]


def test_classification_stage_records_category_evidence(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    records = [normalized_example("a"), normalized_example("b")]
    for record in records:
        record["task_type"] = None
    write_jsonl(source, records)
    output = tmp_path / "classified.jsonl"

    report = classify_jsonl(
        source,
        output,
        source="toprak",
        subset="default",
        report_path=tmp_path / "report.json",
    )

    assert report.counters["accepted"] == 2
    records = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["task_type"] == TaskCategory.TOOL_USE.value
    assert records[0]["metadata"]["classification"]["evidence"] == ["tool_calls_field"]


def test_classification_continues_after_malformed_json(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    source.write_text(
        "not-json\n" + json.dumps(normalized_example("valid")) + "\n",
        encoding="utf-8",
    )
    report = classify_jsonl(
        source,
        tmp_path / "classified.jsonl",
        source="test",
        subset="default",
        report_path=tmp_path / "report.json",
    )

    assert report.counters["malformed"] == 1
    assert report.counters["accepted"] == 1


def test_filter_scores_and_rejects_structural_failures(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    write_jsonl(
        source,
        [
            normalized_example("good"),
            {"messages": []},
            {"messages": [{"role": "user", "content": "only user"}]},
        ],
    )
    output = tmp_path / "filtered.jsonl"
    rejects = tmp_path / "rejects.jsonl"

    report = filter_jsonl(
        source,
        output,
        report_path=tmp_path / "report.json",
        rejects_path=rejects,
        limits={
            "require_user": True,
            "require_assistant": True,
            "minimum_quality_score": 0.0,
        },
    )

    assert report.counters["accepted"] == 1
    assert report.counters["rejected"] == 2
    assert quality_score(normalized_example("x")) > 0.5


def test_quality_filter_counts_tool_arguments_and_flags_review_items() -> None:
    example = normalized_example("large")
    example["messages"][1]["tool_calls"][0]["arguments"] = {"payload": "x" * 100}
    filtered, reason = filter_example(
        example, {"max_chars": 20, "minimum_quality_score": 0.0}
    )

    assert filtered is None
    assert reason == "too_long"

    review = normalized_example("review")
    review["messages"][0]["content"] = "Contact person@example.com"
    filtered, reason = filter_example(review, {"max_chars": 10000})
    assert reason is None
    assert filtered is not None
    assert "sensitive_data_review" in filtered["quality_flags"]

    incomplete = normalized_example("incomplete")
    incomplete["messages"] = incomplete["messages"][:2]
    incomplete["tool_results"] = []
    filtered, reason = filter_example(incomplete, {"max_chars": 10000})
    assert reason is None
    assert filtered is not None
    assert "missing_tool_result" in filtered["quality_flags"]


def test_deduplication_detects_exact_and_near_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    near_copy = normalized_example("near")
    near_copy["messages"][0]["content"] = "goal near with extra words"
    write_jsonl(
        source,
        [normalized_example("same"), normalized_example("same"), near_copy],
    )
    output = tmp_path / "unique.jsonl"
    duplicates = tmp_path / "duplicates.jsonl"

    report = deduplicate_jsonl(
        source,
        output,
        report_path=tmp_path / "report.json",
        duplicates_path=duplicates,
        near_duplicates=True,
        near_threshold=0.7,
    )

    assert report.counters["accepted"] == 1
    assert report.counters["duplicates"] == 2
    reasons = [
        json.loads(line)["reason"]
        for line in duplicates.read_text(encoding="utf-8").splitlines()
    ]
    assert "exact_duplicate" in reasons
    assert "near_duplicate" in reasons


def test_deduplication_uses_content_instead_of_provenance(tmp_path: Path) -> None:
    first = normalized_example("first")
    second = json.loads(json.dumps(first))
    second["id"] = "second"
    second["source"] = "other-source"
    second["metadata"] = {"row_number": 999, "source_file": "other.jsonl"}
    source = tmp_path / "input.jsonl"
    write_jsonl(source, [first, second])
    output = tmp_path / "unique.jsonl"
    duplicates = tmp_path / "duplicates.jsonl"

    report = deduplicate_jsonl(
        source,
        output,
        report_path=tmp_path / "report.json",
        duplicates_path=duplicates,
    )

    assert report.counters["accepted"] == 1
    assert report.counters["duplicates"] == 1
    assert report.counters["exact_duplicate"] == 1


def test_deduplication_detects_normalized_text_duplicates(tmp_path: Path) -> None:
    first = normalized_example("first")
    first["messages"][0]["content"] = "Goal"
    second = normalized_example("second")
    second["messages"][0]["content"] = "  GOAL  "
    for record in (first, second):
        record["tool_calls"] = []
        record["tool_results"] = []
        record["messages"][1].pop("tool_calls", None)
    source = tmp_path / "input.jsonl"
    write_jsonl(source, [first, second])

    report = deduplicate_jsonl(
        source,
        tmp_path / "unique.jsonl",
        report_path=tmp_path / "report.json",
        duplicates_path=tmp_path / "duplicates.jsonl",
    )

    assert report.counters["normalized_duplicate"] == 1


def test_deduplication_across_multiple_inputs(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_jsonl(first, [normalized_example("first")])
    duplicate = json.loads(json.dumps(normalized_example("first")))
    duplicate["id"] = "second"
    duplicate["source"] = "other"
    write_jsonl(second, [duplicate])

    report = deduplicate_jsonl(
        [first, second],
        tmp_path / "unique.jsonl",
        report_path=tmp_path / "report.json",
        duplicates_path=tmp_path / "duplicates.jsonl",
    )

    assert report.counters["accepted"] == 1
    assert report.counters["exact_duplicate"] == 1


def test_deduplication_is_resumable(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    write_jsonl(
        source,
        [normalized_example(str(index)) for index in range(3)],
    )
    output = tmp_path / "unique.jsonl"
    duplicates = tmp_path / "duplicates.jsonl"
    report = tmp_path / "report.json"

    first = deduplicate_jsonl(
        source,
        output,
        report_path=report,
        duplicates_path=duplicates,
        max_records=1,
    )
    second = deduplicate_jsonl(
        source,
        output,
        report_path=report,
        duplicates_path=duplicates,
    )

    assert first.counters["accepted"] == 1
    assert second.counters["accepted"] == 2
    assert len(output.read_text(encoding="utf-8").splitlines()) == 3
    assert not duplicates.exists() or not duplicates.read_text(encoding="utf-8")


def test_split_is_deterministic_and_statistics_stream(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    records = [normalized_example(str(index), "CODING") for index in range(20)]
    write_jsonl(source, records)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    split_jsonl(
        source,
        first_dir,
        report_path=tmp_path / "first-report.json",
        train_ratio=0.8,
        validation_ratio=0.1,
    )
    split_jsonl(
        source,
        second_dir,
        report_path=tmp_path / "second-report.json",
        train_ratio=0.8,
        validation_ratio=0.1,
    )
    for name in ("train", "validation", "test"):
        assert (first_dir / f"{name}.jsonl").read_bytes() == (
            second_dir / f"{name}.jsonl"
        ).read_bytes()

    stats = calculate_statistics(
        source,
        report_path=tmp_path / "stats.json",
    )
    assert stats["accepted"] == 20
    assert stats["categories"]["CODING"] == 20
    assert stats["estimated_tokens"] > 0


def test_mixture_uses_configured_weights(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_jsonl(first, [normalized_example("a", "CODING")])
    write_jsonl(second, [normalized_example("b", "TOOL_USE")])
    output = tmp_path / "mixture.jsonl"

    report = create_mixture(
        [first, second],
        output,
        weights={"CODING": 0.5, "TOOL_USE": 0.5},
        report_path=tmp_path / "report.json",
        max_records=2,
    )

    assert report.counters["accepted"] == 2
    records = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert {record["task_type"] for record in records} == {"CODING", "TOOL_USE"}


def test_mixture_fills_categories_not_explicitly_configured(tmp_path: Path) -> None:
    coding = tmp_path / "coding.jsonl"
    web = tmp_path / "web.jsonl"
    write_jsonl(coding, [normalized_example("code", "CODING")])
    write_jsonl(web, [normalized_example("web", "WEB_TASK")])
    output = tmp_path / "mixture.jsonl"

    report = create_mixture(
        [coding, web],
        output,
        weights={"CODING": 1.0},
        report_path=tmp_path / "report.json",
        max_records=2,
    )

    assert report.counters["accepted"] == 2
    assert {
        json.loads(line)["task_type"]
        for line in output.read_text(encoding="utf-8").splitlines()
    } == {"CODING", "WEB_TASK"}


def test_mixture_canonicalizes_plural_configuration_names(tmp_path: Path) -> None:
    source = tmp_path / "agent.jsonl"
    write_jsonl(source, [normalized_example("agent", "AGENT_TRAJECTORY")])
    output = tmp_path / "mixture.jsonl"

    report = create_mixture(
        [source],
        output,
        weights={"agent_trajectories": 1.0},
        report_path=tmp_path / "report.json",
        max_records=1,
    )

    assert report.counters["accepted"] == 1
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["quotas"]["AGENT_TRAJECTORY"] == 1


class FakeDownloadResponse:
    status = 200

    def __init__(self) -> None:
        self.headers = {"Content-Length": "11"}
        self.closed = False
        self.sent = False

    def read(self, size: int = -1) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        return b"hello world"

    def close(self) -> None:
        self.closed = True


def test_download_requires_explicit_source_selection(tmp_path: Path) -> None:
    config = PipelineConfig.load(Path("configs/dataset.toml"))

    with pytest.raises(PipelineError):
        download_sources(config, tmp_path)


def test_download_is_bounded_and_hashes_output(tmp_path: Path) -> None:
    class Opener:
        def __call__(self, request: Any, *, timeout: float) -> FakeDownloadResponse:
            return FakeDownloadResponse()

    downloader = ResumableDownloader(opener=Opener())
    result = downloader.download(
        "owner/data", "subset/file.jsonl", tmp_path / "file.jsonl"
    )

    assert result.total_bytes == 11
    assert len(result.sha256) == 64
    assert (tmp_path / "file.jsonl").read_bytes() == b"hello world"
    with pytest.raises(PipelineError):
        downloader.download(
            "owner/data", "subset/file.jsonl", tmp_path / "other.jsonl", max_bytes=1
        )


def test_inspection_records_license_and_viewer_error() -> None:
    class FakeClient:
        def dataset_api(self, dataset_id: str) -> dict[str, Any]:
            return {
                "sha": "rev",
                "private": False,
                "gated": False,
                "usedStorage": 42,
                "tags": ["agent"],
                "cardData": {"license": "apache-2.0"},
                "siblings": [{"rfilename": "file.jsonl"}],
            }

        def remote_size(self, dataset_id: str, file_name: str) -> int:
            return 42

        def dataset_server_info(self, dataset_id: str) -> dict[str, Any]:
            return {"dataset_info": {}}

        def first_rows(
            self, dataset_id: str, config: str, split: str
        ) -> dict[str, Any]:
            return {"rows": [{"messages": []}]}

    from data_pipeline.config import SourceConfig

    result = inspect_source(
        "test",
        SourceConfig("owner/data", "jsonl", "apache-2.0", ("file.jsonl",)),
        client=FakeClient(),
    )

    assert result["license"] == "apache-2.0"
    assert result["files"][0]["size_bytes"] == 42
    assert result["viewer"]["sample_status"] == "ok"


def test_prepare_runs_bounded_stages(tmp_path: Path) -> None:
    config_path = tmp_path / "dataset.toml"
    config_path.write_text(
        """[paths]
root = "datasets"
[limits]
max_records = 3
train_ratio = 0.8
validation_ratio = 0.1
[quality]
max_chars = 10000
[mixture]
coding = 1.0
[sources.starcoder]
id = "starcoder-python"
format = "jsonl"
license = "apache-2.0"
files = ["input.jsonl"]
""",
        encoding="utf-8",
    )
    input_path = tmp_path / "input.jsonl"
    write_jsonl(
        input_path,
        [
            {"id": str(index), "instruction": "Write code", "text": f"print({index})"}
            for index in range(3)
        ],
    )
    config = PipelineConfig.load(config_path)
    config.ensure_directories()
    write_json(
        config.manifests / "inventory.json",
        {"datasets": [{"dataset_id": "starcoder-python", "revision": "observed-rev"}]},
    )

    result = prepare_sources(
        config, {"starcoder": input_path}, max_records=3, resume=False
    )

    assert result["final_statistics"]["accepted"] == 3
    assert Path(result["output"]).exists()
    assert len(Path(result["output"]).read_text(encoding="utf-8").splitlines()) == 3
    normalized = json.loads(
        (config.staging / "starcoder" / "normalized.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert normalized["metadata"]["revision"] == "observed-rev"
    assert normalized["metadata"]["subset"] == input_path.parent.name


def test_iter_input_records_reads_jsonl_and_reports_bad_lines(tmp_path: Path) -> None:
    source = tmp_path / "input.jsonl"
    source.write_text('{"id":"ok"}\nnot-json\n', encoding="utf-8")
    rows = list(iter_input_records(source))

    assert rows[0][1] == {"id": "ok"}
    assert rows[1][2] == "malformed JSON record"
