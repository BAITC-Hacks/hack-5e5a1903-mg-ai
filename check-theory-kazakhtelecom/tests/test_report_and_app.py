"""Заключение и веб-экран: из результата собираются markdown, docx и HTML; сервис отвечает по HTTP."""

import time

from orgdiff.report import build_conclusion, data_issues, losses, to_docx, to_markdown


def test_conclusion_has_all_required_sections_and_sources(control_result, tmp_path):
    conc = build_conclusion(control_result, with_recommendations=False)
    md = to_markdown(conc)
    for header in (
        "## 1. Изменения организационной структуры",
        "## 2. Потери функций",
        "## 5. Дублирование функций",
        "## 6. Потенциальные конфликты интересов",
        "## 7. Таблица сопоставления функций",
        "## 8. Сведения о проверке",
    ):
        assert header in md
    assert "рекомендательный характер" in md
    assert "п. 5.6.2" in md and "п. 4.4" in md
    path = to_docx(conc, str(tmp_path / "conclusion.docx"))
    assert (tmp_path / "conclusion.docx").stat().st_size > 10_000, path


def test_empty_clause_from_source_is_a_data_issue_not_a_loss(control_result):
    issues = {(c["doc"], c["id"]) for c in data_issues(control_result)}
    assert any(cid == "5.5.3" for _, cid in issues)
    assert all(f["a"][0]["id"] != "5.5.3" for f in losses(control_result))


def test_full_loss_carries_nearest_candidate_from_new_edition(control_result):
    full = [f for f in losses(control_result) if f["kind"] == "lost"]
    assert full and all(f["candidate"] for f in full)


def test_web_app_end_to_end(before_pdf, before_xlsx, after_pdf, after_xlsx, monkeypatch):
    from fastapi.testclient import TestClient

    import app as webapp

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(webapp.app)
    assert client.get("/").status_code == 200
    assert client.get("/health").json()["status"] == "ok"
    demo = client.get("/demo-files").json()
    assert [x["name"] for x in demo["before"]] == ["положение_ред8.pdf", "структура_ред8.xlsx"]

    files = [
        ("before", (before_pdf.name, before_pdf.read_bytes())),
        ("before", (before_xlsx.name, before_xlsx.read_bytes())),
        ("after", (after_pdf.name, after_pdf.read_bytes())),
        ("after", (after_xlsx.name, after_xlsx.read_bytes())),
    ]
    resp = client.post("/analyze", files=files, data={"golden": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    job = resp.headers["location"].rsplit("/", 1)[-1]
    for _ in range(120):
        st = client.get(f"/job/{job}/status").json()
        if st["state"] in {"done", "error"}:
            break
        time.sleep(1)
    assert st["state"] == "done", st
    report = client.get(f"/job/{job}/report")
    assert report.status_code == 200 and "Потери функций" in report.text and 'class="kpi' in report.text
    assert client.get(f"/job/{job}/conclusion.docx").status_code == 200
    assert client.get(f"/job/{job}/result.json").json()["meta"]["clauses_before"] > 400
    # от каждого вывода можно перейти к пункту в контексте документа
    assert f'href="/job/{job}/doc/before#c-5.6.2"' in report.text
    doc = client.get(f"/job/{job}/doc/before")
    assert doc.status_code == 200 and 'id="c-5.6.2"' in doc.text and "формировать группы контроля качества" in doc.text
    assert client.get(f"/job/{job}/doc/sideways").status_code == 404


def test_unparseable_upload_gives_a_clear_error(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as webapp

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(webapp.app)
    files = [("before", ("a.txt", b"1. text")), ("after", ("b.txt", b"1. text"))]
    job = client.post("/analyze", files=files, follow_redirects=False).headers["location"].rsplit("/", 1)[-1]
    for _ in range(30):
        st = client.get(f"/job/{job}/status").json()
        if st["state"] in {"done", "error"}:
            break
        time.sleep(0.5)
    assert st["state"] == "error" and "неподдерживаемый формат" in st["error"]


def test_llm_client_has_explicit_timeout(monkeypatch):
    from orgdiff.llm import explain_error, llm_timeout, make_client

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ORGDIFF_LLM_TIMEOUT", "7")
    assert llm_timeout() == 7.0
    assert make_client().timeout == 7.0
    assert explain_error(ValueError("x")) is None
