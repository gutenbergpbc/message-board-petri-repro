import pytest
from docent.data_models.chat import parse_chat_message
from inspect_ai.log import read_eval_log, resolve_sample_attachments

from message_board_petri.docent_upload import convert, normalize_metadata
from message_board_petri.harness import ROOT


def test_metadata_normalization_matches_docent_control_character_handling():
    value = {"events": [{"body": "a\\x00b\x00c"}]}
    assert normalize_metadata(value) == {"events": [{"body": "abc"}]}


def test_pilot_upload_preserves_each_samples_judgment():
    paths = sorted(ROOT.glob("runs/pilot-*/logs/*.eval"))
    if not paths:
        pytest.skip("Local pilot logs are not present")
    expected = {}
    for path in paths:
        for sample in read_eval_log(str(path)).samples:
            sample = resolve_sample_attachments(sample)
            judge = [e for e in sample.events if e.event == "model" and e.role == "judge"][-1]
            expected[(str(sample.id), sample.epoch)] = (
                parse_chat_message(judge.output.message.model_dump()).content,
                sample.scores["behavior_score"].value,
            )
    runs = convert(paths)
    assert len(runs) == len(expected)
    for run in runs:
        key = (run.metadata["sample_id"], run.metadata["epoch_id"])
        content, scores = expected[key]
        transcripts = {t.name: t for t in run.transcripts}
        assert set(transcripts) == {"target", "auditor", "judge:behavior"}
        assert transcripts["judge:behavior"].messages[-1].content == content
        assert transcripts["target"].messages != transcripts["auditor"].messages
        assert all(run.metadata[name] == value for name, value in scores.items())
