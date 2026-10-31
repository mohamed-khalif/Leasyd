"""The eval harness itself: the cases are well formed, their telemetry answers the queries that find
the cause, and the scoring tells right from wrong and confident from careful. (Scoring the real model
is a live run: python -m eval.harness --live.)"""
from datetime import datetime, timezone

from eval import harness
from eval.cases import CASES
from eval.fixtures import FixtureTelemetry
from tools.telemetry import ToolRunner

START, END = datetime(2026, 10, 30, 1, 14, tzinfo=timezone.utc), datetime(2026, 10, 30, 2, 34, tzinfo=timezone.utc)


class Script:
    model = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)

    def create(self, **kw):
        return self.turns.pop(0)


def use(i, name, inp):
    return {"content": [{"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp}], "usage": {"input_tokens": 100, "output_tokens": 10}}


def oracle(case, root=None, confidence=0.85):
    """A model that looks where the cause is and then answers `root` (by default the right answer)."""
    exp = case["expect"]
    calls = [("list_services", {})] + list(case["probes"])
    if root is None:
        root = " ".join(g[0] for g in exp["cause"]) if exp["kind"] == "cause" else "not sure"
    cites = [{"id": f"ev{i + 1}", "claim": "shows it"} for i in range(len(calls))]
    submit = {"summary": "s", "root_cause": root, "confidence": confidence, "evidence": cites, "ruled_out": [], "next_step": "n"}
    return Script([use(i, n, a) for i, (n, a) in enumerate(calls)] + [use(99, "submit_investigation", submit)])


def test_cases_are_well_formed():
    ids = [c["id"] for c in CASES]
    assert len(ids) >= 10 and len(set(ids)) == len(ids)
    elsewhere = [c for c in CASES if c["expect"]["kind"] == "not_sure" or "deploy" in c["expect"].get("forbid", [])]
    assert len(elsewhere) >= 4                     # no change found, the cause is elsewhere (or nowhere)
    for c in CASES:
        assert c["expect"]["kind"] in ("cause", "not_sure") and c["alert"]["title"]
        assert c["expect"]["kind"] == "not_sure" or (c["expect"]["cause"] and c["probes"])


def test_each_cases_probes_reach_the_evidence_of_its_cause():
    for c in CASES:
        tel = FixtureTelemetry(c)
        tools = ToolRunner(tel, START, END, "https://app")
        for name, args in c["probes"]:
            text, ev = tools.run(name, args)
            assert ev and tel.log[-1]["key"], (c["id"], name, text[:200])
            assert '"key"' not in text and '"n":' not in text          # the scoring marks never reach the model


def test_the_right_answer_scores_right_on_every_case():
    report = harness.run_all(oracle)
    s = report["summary"]
    assert s["cases"] == len(CASES) and s["accuracy"] == 1.0 and s["false_confidence_rate"] == 0
    assert s["evidence_hit"] == 1.0
    assert all(r["versions"]["model"] == "scripted" for r in report["cases"])


def test_wrong_and_overconfident_answers_are_counted():
    by_id = {c["id"]: c for c in CASES}
    run = lambda cid, *a, **k: harness.run_case(by_id[cid], oracle(by_id[cid], *a, **k))   # noqa: E731
    r = run("dns_elsewhere", "bad deploy of api-gateway: DNS lookup changed")      # names the cause but blames a deploy
    assert not r["correct"] and r["false_confidence"]
    r = run("fraud_timeout", "not sure")                                            # careful, not confident: wrong but honest
    assert not r["correct"] and not r["false_confidence"] and r["confidence"] == 0.3
    r = run("injection", "postgres database")
    assert not r["correct"] and r["false_confidence"]
    r = run("noisy_blip", "a single declined card on three requests", confidence=0.7)   # an accepted honest answer
    assert r["correct"]
    r = run("no_data", "cart is slow", confidence=0.6)
    assert not r["correct"] and r["false_confidence"]


def test_a_recorded_run_replays_to_the_same_result(tmp_path):
    case = CASES[0]
    rec = harness.Recorder(oracle(case))
    first = harness.run_case(case, rec)
    rec.save(str(tmp_path / "r.json"))
    again = harness.run_case(case, harness.Replay(str(tmp_path / "r.json")))
    assert {k: again[k] for k in ("correct", "confidence", "cited", "root_cause")} == {k: first[k] for k in ("correct", "confidence", "cited", "root_cause")}


def test_the_recorded_live_run_still_scores():
    """The saved live run (eval/recordings/), played back through today's code: a change to the checks,
    the tools or the scoring that would have turned right answers wrong shows up here."""
    report = harness.run_all(lambda case: harness.Replay(f"{harness.RECORDINGS}/{case['id']}.json"))
    s = report["summary"]
    assert s["accuracy"] >= 0.9 and s["false_confidence_rate"] <= 0.1 and s["evidence_hit"] >= 0.9, report["summary"]
