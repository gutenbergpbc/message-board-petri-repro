"""Scenario discovery and seed routing, without Docker, datasets, or model calls."""

import asyncio
import json
import shutil
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from message_board_petri import budget, cli, harness, seeds
from message_board_petri.scenarios import SCENARIO_DIR, SCENARIOS, load_scenarios
from message_board_petri.task import TASK_ID


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_is_self_contained(tmp_path, name):
    shutil.copytree(SCENARIO_DIR / name, tmp_path / name)
    discovered = load_scenarios(tmp_path)
    assert discovered == {name: SCENARIOS[name]}
    if discovered[name].adaptive:
        assert not (tmp_path / name / "seed.json").exists()
        return
    seed = seeds.load_seed(tmp_path / name / "seed.json", name)
    assert seed["description"] == discovered[name].description
    assert seed["peer_prompted"] == discovered[name].peer_prompted


def test_new_scenario_is_discovered_without_registry_edits(tmp_path):
    directory = tmp_path / "new-condition"
    directory.mkdir()
    source = (SCENARIO_DIR / "ocr-barter/scenario.py").read_text()
    (directory / "scenario.py").write_text(source.replace('id="ocr-barter"', 'id="new-condition"'))
    assert set(load_scenarios(tmp_path)) == {"new-condition"}


@pytest.mark.parametrize(
    "source, error, message",
    [
        ("SCENARIO = None", TypeError, "Expected a SCENARIO"),
        ((SCENARIO_DIR / "ocr-barter/scenario.py").read_text(), ValueError, "ID must match"),
    ],
)
def test_invalid_definition_is_rejected(tmp_path, source, error, message):
    directory = tmp_path / "invalid"
    directory.mkdir()
    (directory / "scenario.py").write_text(source)
    with pytest.raises(error, match=message):
        load_scenarios(tmp_path)


def test_empty_scenario_directory_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="No scenario definitions"):
        load_scenarios(tmp_path)


def test_legacy_archives_are_not_active():
    assert not any(name.startswith("legacy-") for name in SCENARIOS)
    assert len(list(SCENARIO_DIR.glob("legacy-*/*/seed.json"))) == 12


@pytest.mark.parametrize("name", ["missing", "../ocr-barter", "legacy-cbj/blocked"])
def test_unknown_seed_name_is_rejected(name):
    with pytest.raises(ValueError, match="Unknown scenario"):
        seeds.seed_path(name)


@pytest.mark.parametrize("custom_directory", [False, True])
def test_prepare_generates_reuses_and_forces_correct_seed_path(
    tmp_path, monkeypatch, custom_directory
):
    monkeypatch.setattr(seeds, "SCENARIO_DIR", tmp_path / "scenarios")
    directory = tmp_path / "custom-seeds" if custom_directory else None
    expected = (
        directory / "ocr-barter.json"
        if custom_directory
        else tmp_path / "scenarios/ocr-barter/seed.json"
    )
    calls = []

    async def generate(prompt):
        calls.append(prompt)
        return SimpleNamespace(
            completion=json.dumps(
                {
                    "messages": [
                        {"author": "BUD-2", "body": "EOIR is unreadable."},
                        {"author": "BUD-9", "body": "Please improve the OCR helper."},
                    ]
                }
            ),
            usage=None,
        )

    monkeypatch.setattr(seeds, "get_model", lambda *a, **kw: SimpleNamespace(generate=generate))
    assert seeds.seed_path("ocr-barter", directory) == expected
    asyncio.run(seeds.prepare_seeds(directory, ["ocr-barter"], "mockllm/actor"))
    original = expected.read_bytes()
    assert seeds.load_seed(expected, "ocr-barter")["task_id"] == TASK_ID
    assert len(calls) == 1
    asyncio.run(seeds.prepare_seeds(directory, ["ocr-barter"], "mockllm/actor"))
    assert expected.read_bytes() == original
    assert len(calls) == 1
    asyncio.run(seeds.prepare_seeds(directory, ["ocr-barter"], "mockllm/actor", force=True))
    assert len(calls) == 2
    assert seeds.load_seed(expected, "ocr-barter")["task_id"] == TASK_ID


@pytest.mark.parametrize("custom_directory", [False, True])
def test_cli_prepare_reuses_seeds_from_any_cwd(tmp_path, monkeypatch, capsys, custom_directory):
    args = [
        "message-board",
        "--config",
        str(harness.ROOT / "configs/extra-scenarios.yaml"),
        "prepare",
    ]
    if custom_directory:
        for name in (n for n, s in SCENARIOS.items() if not s.adaptive):
            shutil.copyfile(seeds.seed_path(name), tmp_path / f"{name}.json")
        args += ["--seed-dir", str(tmp_path)]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)

    def unexpected_model(*args, **kwargs):
        pytest.fail("Reusing frozen seeds must not call a model")

    monkeypatch.setattr(seeds, "get_model", unexpected_model)
    cli.main()
    output = capsys.readouterr().out
    assert output.count("Reusing") == 3
    expected = (
        tmp_path / "ocr-request-only.json"
        if custom_directory
        else seeds.seed_path("ocr-request-only")
    )
    assert str(expected) in output


@pytest.mark.parametrize("custom_directory", [False, True])
def test_harness_uses_scenario_local_seeds_or_explicit_overrides(
    tmp_path, monkeypatch, custom_directory
):
    bundle = {
        "task_id": TASK_ID,
        "archive_name": "test.tar.gz",
        "archive_sha256": "unused",
        "manifest": {"sources": []},
    }
    monkeypatch.setattr(harness, "load_bundle", lambda _: bundle)
    monkeypatch.setattr(budget, "validate_reference", lambda _: None)
    directory = None
    if custom_directory:
        directory = str(tmp_path)
        for name in (n for n, s in SCENARIOS.items() if not s.adaptive):
            seed = seeds.load_seed(seeds.seed_path(name), name)
            seed["actor_model"] = "custom-override"
            seed["sha256"] = seeds.seed_digest(seed)
            (tmp_path / f"{name}.json").write_text(json.dumps(seed))
    monkeypatch.chdir(tmp_path)
    task = harness.seeded_board(
        seed_dir=directory,
        scenarios=[n for n, s in SCENARIOS.items() if not s.adaptive],
        dataset_dir=str(tmp_path),
        use_judge=False,
        token_budgets={"ocr-barter-token-budget": 100},
    )
    for sample in task.dataset:
        assert sample.metadata["scenario_spec"] == asdict(SCENARIOS[sample.id])
        path = tmp_path / f"{sample.id}.json" if custom_directory else seeds.seed_path(sample.id)
        expected = seeds.load_seed(path, sample.id)
        assert sample.metadata["seed"] == expected


@pytest.mark.parametrize("custom_directory", [False, True])
def test_cli_dry_run_routes_seed_paths(tmp_path, monkeypatch, capsys, custom_directory):
    args = ["message-board", "run", "--dry-run", "--artifact-backend", "synthetic"]
    expected = seeds.load_seed(seeds.seed_path("ocr-barter"), "ocr-barter")
    if custom_directory:
        expected["actor_model"] = "custom-override"
        expected["sha256"] = seeds.seed_digest(expected)
        (tmp_path / "ocr-barter.json").write_text(json.dumps(expected))
        args += ["--seed-dir", str(tmp_path)]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "load_bundle",
        lambda _: {
            "task_id": TASK_ID,
            "archive_name": "test.tar.gz",
            "archive_sha256": "unused",
            "file_count": 0,
            "manifest": {"sources": []},
        },
    )
    monkeypatch.setattr(budget, "validate_reference", lambda _: None)
    calls = []

    def make_task(seed_dir, names, turns, **kwargs):
        calls.append((seed_dir, names))
        return object()

    def unexpected_run(*args, **kwargs):
        pytest.fail("Dry runs must not start infrastructure or model evaluations")

    monkeypatch.setattr(cli, "seeded_board", make_task)
    monkeypatch.setattr(cli, "ensure_running", unexpected_run)
    monkeypatch.setattr(cli, "inspect_eval", unexpected_run)
    cli.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["seeds"] == {"ocr-barter": expected["sha256"]}
    assert calls == [(str(tmp_path) if custom_directory else None, ["ocr-barter"])]
