"""Веб-экран для демонстрации: загрузил комплекты «до» и «после», получил отчёт.

    uv run app.py            # http://localhost:8765

Страница загрузки с зонами перетаскивания, задача выполняется в фоне, страница ожидания
показывает этапы и счётчик обращений к модели, отчёт со сводкой, навигацией, блоками
«было / стало», источниками и цитатами, выгрузка заключения в docx и результата в JSON.
Без фронтенд-фреймворка: HTML собирается на сервере, немного ванильного JS.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

HERE = Path(__file__).parent
JOBS_DIR = HERE / ".cache" / "jobs"
TESTDATA = HERE / "testdata"
TZ = HERE.parent / "TZ"
JOBS: dict[str, dict] = {}
MODEL = os.environ.get("ORGDIFF_MODEL", "gpt-4.1-mini")

app = FastAPI(title="Анализ оргструктуры и функционала")


def load_env():
    """Ключ берётся из окружения, иначе из .env в этой папке, иначе из .env уровнем выше."""
    for env in (HERE / ".env", HERE.parent / ".env"):
        if env.exists() and not os.environ.get("OPENAI_API_KEY"):
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("OPENAI_API_KEY=") and len(line) > 15:
                    os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()


load_env()

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c1f24;--muted:#6b7280;--line:#e5e7eb;--brand:#2b5fd9;--brand2:#1d4ed8;--ok:#15803d;--bad:#b91c1c;--warn:#b45309}
*{box-sizing:border-box}body{font-family:Inter,system-ui,Segoe UI,Arial;margin:0;background:var(--bg);color:var(--ink);line-height:1.5}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 60px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}
header.top h1{font-size:22px;margin:0}header.top .sub{color:var(--muted);font-size:14px;margin-top:2px}
.pill{display:inline-block;font-size:12px;padding:3px 10px;border-radius:999px;background:#eef2ff;color:#3730a3}
.pill.ok{background:#dcfce7;color:#166534}.pill.bad{background:#fee2e2;color:#991b1b}
h2{font-size:18px;margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}h3{font-size:15px;margin:18px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:800px){.grid2{grid-template-columns:1fr}}
.drop{border:2px dashed #c7cdd8;border-radius:14px;padding:22px;text-align:center;background:#fff;cursor:pointer;transition:.15s;min-height:150px}
.drop.over{border-color:var(--brand);background:#eef3ff}.drop .big{font-size:15px;font-weight:600}.drop .hint{color:var(--muted);font-size:13px;margin-top:4px}
.drop input{display:none}
.files{list-style:none;padding:0;margin:10px 0 0;text-align:left}.files li{display:flex;align-items:center;gap:8px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;margin:6px 0;background:#fafbfc;font-size:13px}
.badge{font-size:11px;font-weight:700;padding:2px 7px;border-radius:6px;color:#fff;letter-spacing:.3px}.b-docx{background:#2563eb}.b-pdf{background:#dc2626}.b-xlsx{background:#16a34a}.b-x{background:#6b7280}
.files .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.files .size{color:var(--muted)}.files button{border:0;background:transparent;color:var(--muted);cursor:pointer;font-size:16px}
.err{color:var(--bad);font-size:13px;min-height:18px;margin-top:6px}
.opts{display:flex;flex-wrap:wrap;gap:18px;margin:18px 0 6px}.opt{display:flex;align-items:center;gap:10px;font-size:14px}
.switch{position:relative;width:42px;height:24px;flex:none}.switch input{opacity:0;width:0;height:0}.slider{position:absolute;inset:0;background:#cbd5e1;border-radius:24px;transition:.2s}
.slider:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}.switch input:checked+.slider{background:var(--brand)}.switch input:checked+.slider:before{transform:translateX(18px)}
.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:18px}
.btn{padding:11px 20px;font-size:15px;background:var(--brand);color:#fff;border:0;border-radius:9px;cursor:pointer;font-weight:600}.btn:hover{background:var(--brand2)}.btn:disabled{background:#9db1e8;cursor:not-allowed}
.btn.ghost{background:#fff;color:var(--brand);border:1px solid #c7d2fe}.btn.ghost:hover{background:#eef2ff}
.muted{color:var(--muted)}.small{font-size:13px}
.steps{list-style:none;padding:0;margin:14px 0}.steps li{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line)}
.steps .dot{width:22px;height:22px;border-radius:50%;border:2px solid #cbd5e1;flex:none;display:grid;place-items:center;font-size:12px;color:#fff}
.steps li.done .dot{background:var(--ok);border-color:var(--ok)}.steps li.active .dot{border-color:var(--brand);background:var(--brand);animation:pulse 1.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(43,95,217,.45)}100%{box-shadow:0 0 0 10px rgba(43,95,217,0)}}
.steps .t{font-weight:600}.steps .d{color:var(--muted);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}.kpi{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 14px}.kpi .n{font-size:26px;font-weight:700}.kpi .l{color:var(--muted);font-size:13px}
.kpi.red .n{color:var(--bad)}.kpi.amber .n{color:var(--warn)}.kpi.green .n{color:var(--ok)}
nav.toc{position:sticky;top:0;background:var(--bg);padding:10px 0;z-index:5;display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--line)}nav.toc a{font-size:13px;text-decoration:none;color:#374151;background:#fff;border:1px solid var(--line);padding:5px 10px;border-radius:999px}nav.toc a:hover{border-color:var(--brand);color:var(--brand)}
.was,.now{padding:10px 12px;border-radius:8px;margin:8px 0;font-size:14px}.was{background:#fff1f2}.now{background:#f0fdf4}.now.gray{background:#f3f4f6}
.src{color:var(--muted);font-size:12px}.src a{color:inherit;text-decoration:underline dotted}.src a:hover{color:var(--brand)}.lost{background:#fecaca;padding:0 3px;border-radius:3px}
.tag{display:inline-block;font-size:12px;padding:2px 9px;border-radius:999px;background:#e0e7ff;color:#3730a3;margin-right:8px}
.rec{border-left:3px solid var(--brand);padding:8px 12px;margin:10px 0 2px;background:#eef2ff;border-radius:0 8px 8px 0;font-size:14px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}td,th{border:1px solid var(--line);padding:6px 8px;vertical-align:top}th{background:#f3f4f6;text-align:left}
details{margin:8px 0}summary{cursor:pointer;font-weight:600;padding:6px 0}
.golden{padding:12px 16px;border-radius:12px;margin:12px 0}.golden.good{background:#dcfce7;border:1px solid #86efac}.golden.meh{background:#fef3c7;border:1px solid #fcd34d}
.chk{display:inline-block;font-size:12px;margin:2px 6px 2px 0}.chk.ok{color:var(--ok)}.chk.fail{color:var(--bad)}
"""

INDEX = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Сопоставление оргдокументов</title><style>{css}</style></head><body><div class="wrap">
<header class="top"><div><h1>Сопоставление организационных документов</h1><div class="sub">Загрузите комплект «до» и комплект «после» реорганизации. Агент найдёт изменения структуры, потери и дубли функций, конфликты интересов и соберёт заключение со ссылками на пункты.</div></div>
<span class="pill {key_cls}">{key_state}</span></header>
<form id="f" method="post" action="/analyze" enctype="multipart/form-data">
<div class="grid2">
  <div><div class="drop" id="dz-before"><input type="file" name="before" multiple accept=".docx,.pdf,.xlsx"><div class="big">Комплект «до»</div><div class="hint">Перетащите файлы сюда или нажмите. docx, pdf, xlsx</div><ul class="files"></ul></div><div class="err" id="err-before"></div></div>
  <div><div class="drop" id="dz-after"><input type="file" name="after" multiple accept=".docx,.pdf,.xlsx"><div class="big">Комплект «после»</div><div class="hint">Перетащите файлы сюда или нажмите. docx, pdf, xlsx</div><ul class="files"></ul></div><div class="err" id="err-after"></div></div>
</div>
<div class="opts">
  <label class="opt"><span class="switch"><input type="checkbox" name="verify" value="1" {verify_checked}><span class="slider"></span></span>Проверять спорные места моделью <span class="muted small">gpt-4.1-mini, около 3 минут и $0.05</span></label>
  <label class="opt"><span class="switch"><input type="checkbox" name="golden" value="1"><span class="slider"></span></span>Это контрольный комплект <span class="muted small">показать сверку с известными изменениями</span></label>
</div>
<div class="actions"><button class="btn" type="submit" id="go" disabled>Сравнить документы</button>
<button class="btn ghost" type="button" id="demo">Взять контрольный комплект организаторов</button>
<span class="muted small">В каждом комплекте нужен хотя бы один текстовый документ (docx или pdf). Excel добавляет оргструктуру.</span></div>
</form>
<p class="muted small" style="margin-top:26px">Выводы носят рекомендательный характер. Каждый вывод сопровождается ссылкой на пункт и дословной цитатой; вердикты модели без подтверждённой цитаты в отчёт не попадают.</p>
</div>
<script>
const zones={{before:document.getElementById('dz-before'),after:document.getElementById('dz-after')}};
const state={{before:[],after:[]}};
const fmt=n=>n>1048576?(n/1048576).toFixed(1)+' МБ':Math.max(1,Math.round(n/1024))+' КБ';
const ext=f=>(f.name.split('.').pop()||'').toLowerCase();
function render(side){{
  const z=zones[side];const ul=z.querySelector('.files');ul.innerHTML='';
  state[side].forEach((f,i)=>{{const e=ext(f);const li=document.createElement('li');
    li.innerHTML=`<span class="badge b-${{['docx','pdf','xlsx'].includes(e)?e:'x'}}">${{e.toUpperCase()}}</span><span class="name" title="${{f.name}}">${{f.name}}</span><span class="size">${{fmt(f.size)}}</span><button type="button" title="Убрать">×</button>`;
    li.querySelector('button').onclick=ev=>{{ev.stopPropagation();state[side].splice(i,1);sync(side)}};ul.appendChild(li)}});
  const dt=new DataTransfer();state[side].forEach(f=>dt.items.add(f));z.querySelector('input').files=dt.files;
}}
function validate(){{let ok=true;for(const side of ['before','after']){{const fs=state[side];const err=document.getElementById('err-'+side);
  const bad=fs.filter(f=>!['docx','pdf','xlsx'].includes(ext(f)));const text=fs.some(f=>['docx','pdf'].includes(ext(f)));
  err.textContent=bad.length?'Неподдерживаемый формат: '+bad.map(f=>f.name).join(', '):(fs.length&&!text?'Нужен хотя бы один docx или pdf':'');
  if(!fs.length||bad.length||!text)ok=false}}document.getElementById('go').disabled=!ok;return ok}}
function add(side,list){{for(const f of list){{if(!state[side].some(x=>x.name===f.name&&x.size===f.size))state[side].push(f)}}sync(side)}}
function sync(side){{render(side);validate()}}
for(const side of ['before','after']){{const z=zones[side];const inp=z.querySelector('input');
  z.addEventListener('click',e=>{{if(e.target.tagName!=='BUTTON')inp.click()}});
  inp.addEventListener('change',()=>{{add(side,inp.files)}});
  ['dragenter','dragover'].forEach(ev=>z.addEventListener(ev,e=>{{e.preventDefault();z.classList.add('over')}}));
  ['dragleave','drop'].forEach(ev=>z.addEventListener(ev,e=>{{e.preventDefault();z.classList.remove('over')}}));
  z.addEventListener('drop',e=>add(side,e.dataTransfer.files));}}
document.getElementById('f').addEventListener('submit',e=>{{if(!validate()){{e.preventDefault();return}}document.getElementById('go').textContent='Загружаем…';document.getElementById('go').disabled=true}});
document.getElementById('demo').onclick=async()=>{{const b=document.getElementById('demo');b.disabled=true;b.textContent='Загружаем контрольный комплект…';
  const r=await fetch('/demo-files');const j=await r.json();
  for(const side of ['before','after']){{state[side]=[];for(const it of j[side]){{const resp=await fetch(it.url);const blob=await resp.blob();state[side].push(new File([blob],it.name))}}sync(side)}}
  document.querySelector('input[name=golden]').checked=true;b.textContent='Контрольный комплект загружен';}};
</script></body></html>"""

STAGES = [
    ("Файлы загружены", "Загрузка", "Файлы получены сервером"),
    ("Разбор документов", "Разбор", "Пункты, владельцы, модальность; PDF склеивается в абзацы"),
    ("Близость", "Близость", "Лексика и эмбеддинги"),
    ("Выравнивание пунктов", "Выравнивание", "Динамическое программирование со слияниями"),
    ("Проверка спорных мест", "Проверка моделью", "Голосование трёх вызовов, цитаты сверяются с текстом"),
    ("Подтверждение дублей", "Дубли", "Кластеры подтверждает модель"),
    ("Формирование заключения", "Заключение", "Рекомендации и docx"),
]

JOB_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Анализ</title><style>{css}</style></head><body><div class="wrap">
<header class="top"><div><h1>Анализ выполняется</h1><div class="sub">С проверкой моделью это занимает около трёх минут. Страница обновится сама.</div></div><span class="pill" id="elapsed">0:00</span></header>
<div class="card"><ul class="steps" id="steps">{steps}</ul><div class="muted small" id="last"></div></div>
<p class="muted small"><a href="/">← Новый анализ</a></p></div>
<script>
const stages={stages_json};const t0=Date.now();let fails=0;
function tick(){{const s=Math.floor((Date.now()-t0)/1000);document.getElementById('elapsed').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}}setInterval(tick,1000);
function paint(log){{let idx=-1;for(const line of log){{stages.forEach((st,i)=>{{if(line.startsWith(st[0]))idx=Math.max(idx,i)}})}}
  const lis=document.querySelectorAll('#steps li');lis.forEach((li,i)=>{{li.className=i<idx?'done':(i===idx?'active':'')}});
  const last=log[log.length-1]||'';document.getElementById('last').textContent=last;}}
async function poll(){{let j=null;try{{const r=await fetch('/job/{job}/status');if(r.ok)j=await r.json()}}catch(e){{}}
  if(!j||j.state==='missing'){{fails++;if(fails>3){{document.getElementById('last').textContent='Задача потеряна: сервер был перезапущен во время анализа. Запустите анализ заново.';return}}setTimeout(poll,2000);return}}
  fails=0;paint(j.log||[]);
  if(j.state==='done'){{document.querySelectorAll('#steps li').forEach(li=>li.className='done');location.href='/job/{job}/report';return}}
  if(j.state==='error'){{document.getElementById('last').innerHTML='<span style="color:#b91c1c">Ошибка: '+j.error+'</span>';return}}
  setTimeout(poll,1500)}}
poll();
</script></body></html>"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def mark_lost(text: str, lost: str) -> str:
    t = esc(text)
    if lost:
        q = esc(lost)
        if q in t:
            t = t.replace(q, f'<span class="lost">{q}</span>', 1)
    return t


def render_report(job: str, result: dict, conc: dict) -> str:
    from orgdiff.report import COI_LABELS, DISCLAIMER, MOVE_KINDS, NEW_KINDS, data_issues, lost_part, redistributed, rep

    m, s, recs = result["meta"], result["structure"], conc["recs"]
    losses = conc["losses"]
    ab = set(s.get("positions_removed", []))
    moves = [
        f for f in result["findings"] if f["kind"] in MOVE_KINDS and f["a"] and rep(f)["section"] in {"4", "5"} and (rep(f)["owner"] or "") not in ab
    ]
    news = [f for f in result["findings"] if f["kind"] in NEW_KINDS and f["b"] and rep(f, "b")["section"] in {"4", "5"}]
    redis = redistributed(result)
    issues = data_issues(result)

    out = [
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Отчёт</title><style>{CSS}</style></head><body><div class="wrap">'
    ]
    out.append(
        '<header class="top"><div><h1>Результат сопоставления</h1>'
        f'<div class="sub">До: {esc(", ".join(m["before"]))} · После: {esc(", ".join(m["after"]))}</div></div>'
        f'<div><a class="btn" href="/job/{job}/conclusion.docx">Скачать заключение .docx</a></div></header>'
    )
    out.append(
        f'<p class="muted small">Пунктов {m["clauses_before"]} / {m["clauses_after"]} · режим {esc(m["mode"])} · '
        + (
            f"модель {esc(m['model'])}, обращений {m['llm_calls']}, ~${m['llm_cost_usd']}, отклонено вердиктов без цитаты: {m['bad_quotes_blocked']}"
            if m["verify"]
            else "без проверки моделью"
        )
        + f' · {m["seconds"]} с · <a href="/job/{job}/result.json">JSON</a> · <a href="/">новый анализ</a></p>'
    )

    if "golden" in result:
        g = result["golden"]
        good = g["recall"] >= 0.9
        out.append(
            f'<div class="golden {"good" if good else "meh"}"><b>Сверка с контрольными изменениями:</b> полнота <b>{g["recall"] * 100:.0f}%</b> ({g["found"]} из {g["total"]}), точность по потерям <b>{g["precision_loss"] * 100:.0f}%</b>, F1 {g["f1"] * 100:.0f}%.'
            + (f" Пропущено: {esc(', '.join(g['missed']))}." if g["missed"] else " Пропусков нет.")
            + (f" Спорные: {esc(', '.join(g['loss_fp']))}." if g["loss_fp"] else "")
            + '<details><summary class="small">Все проверки</summary>'
            + " ".join(f'<span class="chk {"ok" if c["ok"] else "fail"}">{"✓" if c["ok"] else "✗"} {esc(c["name"])}</span>' for c in g["checks"])
            + "</details></div>"
        )

    n_lost = sum(1 for f in losses if f["kind"] == "lost")
    n_part = sum(1 for f in losses if f["kind"] == "partial_loss")
    n_rem = sum(1 for f in losses if f["kind"] == "removed_from_owner")
    out.append(
        '<div class="kpis">'
        f'<div class="kpi red"><div class="n">{n_lost}</div><div class="l">потерь функций</div></div>'
        f'<div class="kpi amber"><div class="n">{n_part}</div><div class="l">частичных потерь</div></div>'
        f'<div class="kpi amber"><div class="n">{n_rem}</div><div class="l">снято с владельца</div></div>'
        f'<div class="kpi"><div class="n">{len(result["duplicates"])}</div><div class="l">дублирований</div></div>'
        f'<div class="kpi"><div class="n">{len(result["coi"])}</div><div class="l">конфликт интересов</div></div>'
        f'<div class="kpi"><div class="n">{len(moves)}</div><div class="l">переносов и слияний</div></div>'
        f'<div class="kpi"><div class="n">{len(news)}</div><div class="l">новых функций</div></div>'
        f'<div class="kpi green"><div class="n">{result["counts"].get("unchanged", 0)}</div><div class="l">без изменений</div></div></div>'
    )
    out.append(
        '<nav class="toc"><a href="#s1">Структура</a><a href="#s2">Потери</a><a href="#s3">Переносы</a><a href="#s4">Новые</a><a href="#s5">Дубли</a><a href="#s6">Конфликт интересов</a><a href="#s7">Таблица</a></nav>'
    )
    out.append(f'<p class="muted small"><i>{esc(DISCLAIMER)}</i></p>')

    out.append('<h2 id="s1">1. Организационная структура</h2><div class="card">')
    nb, na = s["names_before"], s["names_after"]
    for a in s["departments_created"]:
        out.append(
            f'<div>✚ <b>Создано:</b> {esc(na.get(a, a))} ({esc(a)}) <span class="src">после, п. {esc(s["sources_after"].get(a, "3.4"))}</span></div>'
        )
    for a in s["departments_removed"]:
        out.append(
            f'<div>✖ <b>Упразднено:</b> {esc(nb.get(a, a))} ({esc(a)}) <span class="src">до, п. {esc(s["sources_before"].get(a, "3.4"))}</span></div>'
        )
    for a in s["departments_kept"]:
        out.append(f'<div class="muted">= Сохранено: {esc(na.get(a, a))} ({esc(a)})</div>')
    for p in s["positions_removed"]:
        out.append(f'<div>✖ <b>Упразднена должность:</b> {esc(p)} <span class="src">до, п. 3.5</span></div>')
    for p in s["positions_created"]:
        out.append(f'<div>✚ <b>Введена должность:</b> {esc(p)} <span class="src">после, п. 3.5</span></div>')
    rem = [x for x in s.get("unit_positions_removed", []) if x[0] != "Главный аудитор"]
    add = [x for x in s.get("unit_positions_created", []) if x[0] != "Главный аудитор"]
    if rem or add:
        out.append(f'<details><summary class="small">Изменения состава должностей внутри подразделений: −{len(rem)} / +{len(add)}</summary>')
        for boss, p in rem:
            out.append(
                f'<div class="muted small">− «{esc(p)}» у {esc(boss)} <span class="src">до, п. {esc(s["sources_before"].get(boss + ":" + p, "3"))}</span></div>'
            )
        for boss, p in add:
            out.append(
                f'<div class="muted small">+ «{esc(p)}» у {esc(boss)} <span class="src">после, п. {esc(s["sources_after"].get(boss + ":" + p, "3"))}</span></div>'
            )
        out.append("</details>")
    out.append("</div>")
    if redis:
        out.append(f'<details><summary>Перераспределение функций упразднённой должности ({len(redis)})</summary><div class="card">')
        for f in redis:
            a, b = rep(f), rep(f, "b")
            out.append(
                f'<div class="small">«{esc(a["text"][:110])}» <span class="src">{esc(a["doc"])}, п. {esc(a["id"])}, {esc(a["owner"])}</span> → '
                + (f'<span class="src">{esc(b["doc"])}, п. {esc(b["id"])}, {esc(b["owner"] or "")}</span>' if b else "получатель не найден")
                + f" · {esc(f['title'])}</div>"
            )
        out.append("</div></details>")

    out.append('<h2 id="s2">2. Потери функций</h2>')
    if not losses:
        out.append('<div class="card">Потерь функций не выявлено.</div>')
    for f in losses:
        a, b = rep(f), rep(f, "b")
        lp = lost_part(f)
        d = f.get("llm") or {}
        out.append(
            f'<div class="card"><span class="tag">{esc(f["title"])}</span><b>{esc(a["owner"] or "без владельца")}</b> <span class="src">{esc(a["doc"])}, п. {esc(a["id"])}</span>'
        )
        out.append(f'<div class="was"><span class="src">Было · {esc(a["doc"])}, п. {esc(a["id"])}</span><br>{mark_lost(a["text"], lp)}</div>')
        if b:
            out.append(
                f'<div class="now"><span class="src">Стало · {esc(b["doc"])}, п. {esc(b["id"])} · {esc(b["owner"] or "")}</span><br>{esc(b["text"])}</div>'
            )
        elif f["kind"] == "lost":
            cand = f.get("candidate")
            out.append(
                '<div class="now gray"><span class="src">Стало</span><br>В новой редакции пункта с этой функцией нет.'
                + (
                    f'<br><span class="src">Ближайший по тексту пункт, функцию не покрывает · {esc(cand["doc"])}, п. {esc(cand["id"])}, близость {f["sim"]:.2f}</span><br><span class="muted small">{esc(cand["text"][:220])}</span>'
                    if cand
                    else ""
                )
                + "</div>"
            )
        if lp:
            out.append(f"<div><b>Пропало:</b> «{esc(lp)}»</div>")
        if d.get("reason"):
            out.append(
                f'<div class="muted small">Обоснование модели: {esc(d["reason"])}'
                + (f" · голоса {esc(d['votes'])}" if d.get("votes") else "")
                + "</div>"
            )
        if a["id"] in recs:
            out.append(f'<div class="rec"><b>Рекомендация:</b> {esc(recs[a["id"]])}</div>')
        out.append("</div>")

    out.append('<h2 id="s3">3. Переносы, слияния и смена владельца</h2>')
    if not moves:
        out.append('<div class="card">Не выявлено.</div>')
    else:
        out.append("<table><tr><th>Вид</th><th>Было</th><th>Стало</th></tr>")
        for f in moves:
            a, b = rep(f), rep(f, "b")
            out.append(
                f'<tr><td>{esc(f["title"])}</td><td><span class="src">{esc(a["doc"])}, п. {esc(a["id"])} · {esc(a["owner"] or "")}</span><br>{esc(a["text"][:200])}</td>'
                f"<td>{('<span class=src>' + esc(b['doc']) + ', п. ' + esc(b['id']) + ' · ' + esc(b['owner'] or '') + '</span><br>' + esc(b['text'][:200])) if b else '—'}</td></tr>"
            )
        out.append("</table>")

    out.append('<h2 id="s4">4. Новые функции</h2>')
    if not news:
        out.append('<div class="card">Не выявлено.</div>')
    for f in news:
        b = rep(f, "b")
        out.append(
            f'<div class="card"><span class="src">{esc(b["doc"])}, п. {esc(b["id"])} · {esc(b["owner"] or "")}</span><br>{esc(b["text"])}</div>'
        )

    out.append('<h2 id="s5">5. Дублирование функций</h2>')
    if not result["duplicates"]:
        out.append('<div class="card">Не выявлено.</div>')
    for g in result["duplicates"]:
        v = g.get("verdict") or {}
        title = {"same": "Одна и та же функция", "overlap": "Пересечение функций"}.get(v.get("verdict"), "Похожие функции")
        out.append(f'<div class="card"><span class="tag">{esc(title)}</span><b>{esc(v.get("shared_function") or "")}</b>')
        for c in g["clauses"]:
            out.append(
                f'<div class="now"><span class="src">{esc(c["doc"])}, п. {esc(c["id"])} · {esc(c["owner"] or "")}</span><br>{esc(c["text"])}</div>'
            )
        if v.get("reason"):
            out.append(f'<div class="muted small">Обоснование модели: {esc(v["reason"])}</div>')
        if g["clauses"][0]["id"] in recs:
            out.append(f'<div class="rec"><b>Рекомендация:</b> {esc(recs[g["clauses"][0]["id"]])}</div>')
        out.append("</div>")

    out.append('<h2 id="s6">6. Потенциальные конфликты интересов</h2>')
    if not result["coi"]:
        out.append('<div class="card">Не выявлено.</div>')
    for c in result["coi"]:
        v = c.get("verdict")
        tag = (
            f' <span class="pill {"bad" if v["verdict"] == "risk" else "ok" if v["verdict"] == "safeguard" else ""}">{esc(COI_LABELS[v["verdict"]])}</span>'
            if v
            else ""
        )
        basis = (
            f'<div class="rec"><b>Основание:</b> «{esc(v["quote"])}». {esc(v["reason"])}</div>'
            if v and v.get("quotes_ok") and v["verdict"] != "mention"
            else ""
        )
        out.append(
            f'<div class="card"><span class="src">{esc(c["doc"])}, п. {esc(c["id"])} · {esc(c["owner"] or "")}</span>{tag}<br>{esc(c["text"])}{basis}'
            + (f'<div class="rec"><b>Рекомендация:</b> {esc(recs[c["id"]])}</div>' if c["id"] in recs else "")
            + "</div>"
        )

    if issues:
        out.append('<h2>Замечания к исходным документам</h2><div class="card">')
        for c in issues:
            out.append(
                f'<div class="small">{esc(c["doc"])}, п. {esc(c["id"])}: пункт пустой, в документе стоит «{esc(c["text"])}». Дефект исходника, в анализе функций не участвует.</div>'
            )
        out.append("</div>")

    out.append(
        f'<h2 id="s7">7. Таблица сопоставления функций</h2><details><summary>Изменившиеся пункты: {len(conc["table"])}</summary><table><tr><th>Вид</th><th>До</th><th>Владелец</th><th>После</th><th>Владелец</th><th>Близость</th></tr>'
    )
    for row in conc["table"]:
        out.append(
            f"<tr><td>{esc(row['kind'])}</td><td>{esc(row['before'])}</td><td>{esc(row['before_owner'])}</td><td>{esc(row['after'])}</td><td>{esc(row['after_owner'])}</td><td>{row['sim']:.2f}</td></tr>"
        )
    out.append("</table></details></div></body></html>")
    return link_sources(job, "\n".join(out))


SRC_REF = re.compile(r"(?<![\wа-яё])(до|после)(:[^<,]*)?, п\. ([0-9][0-9A-Za-zА-Яа-яё.()]*?)(?=[\s<,;·])")


def link_sources(job: str, html_text: str) -> str:
    """Каждая ссылка на источник «до, п. 5.6.2» ведёт на пункт в контексте документа."""

    def repl(m):
        side = "before" if m.group(1) == "до" else "after"
        cid = m.group(3).rstrip(".")
        return f'<a href="/job/{job}/doc/{side}#c-{cid}" title="показать пункт в документе">{m.group(1)}{m.group(2) or ""}, п. {cid}</a>'

    return SRC_REF.sub(repl, html_text)


DOC_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{css}
.clause{{padding:6px 10px;border-left:3px solid transparent;margin:2px 0}}.clause:target{{background:#fff7d6;border-left-color:#b45309}}
.clause .id{{display:inline-block;min-width:74px;font-weight:600;color:#374151}}.clause.lead{{font-weight:600;margin-top:10px}}.clause .own{{color:var(--muted);font-size:12px;margin-left:8px}}
</style></head><body><div class="wrap"><header class="top"><div><h1>{title}</h1><div class="sub">{sub}</div></div><a class="btn ghost" href="/job/{job}/report">← к отчёту</a></header>
{body}</div></body></html>"""


def render_doc(job: str, side: str, result: dict) -> str:
    clauses = result.get("clauses", {}).get(side, [])
    docs = sorted({c["doc"] for c in clauses})
    rows = []
    for c in clauses:
        rows.append(
            f'<div class="clause{" lead" if c.get("lead") else ""}" id="c-{esc(c["id"])}"><span class="id">{esc(c["id"])}</span>{esc(c["text"])}'
            + (f'<span class="own">{esc(c["owner"])}</span>' if c.get("owner") else "")
            + "</div>"
        )
    title = "Комплект «до»" if side == "before" else "Комплект «после»"
    return DOC_PAGE.format(css=CSS, job=job, title=title, sub=esc(", ".join(docs)) + f" · пунктов: {len(clauses)}", body="\n".join(rows))


# ---------------------------------------------------------------- маршруты


@app.get("/", response_class=HTMLResponse)
def index():
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    return INDEX.format(
        css=CSS,
        key_state="Ключ OpenAI найден" if has_key else "Ключ OpenAI не найден, только близость текстов",
        key_cls="ok" if has_key else "bad",
        verify_checked="checked" if has_key else "",
    )


DEMO = {
    "before": [("положение_ред8.pdf", TESTDATA / "положение_ред8.pdf"), ("структура_ред8.xlsx", TESTDATA / "структура_ред8.xlsx")],
    "after": [("положение_ред9.pdf", TESTDATA / "положение_ред9.pdf"), ("структура_ред9.xlsx", TESTDATA / "структура_ред9.xlsx")],
}


@app.get("/demo-files")
def demo_files():
    return {side: [{"name": n, "url": f"/demo-file/{side}/{i}"} for i, (n, p) in enumerate(items) if p.exists()] for side, items in DEMO.items()}


@app.get("/demo-file/{side}/{i}")
def demo_file(side: str, i: int):
    n, p = DEMO[side][i]
    return FileResponse(p, filename=n)


def _worker(job: str, before: list[Path], after: list[Path], verify: bool, golden: bool):
    from orgdiff.pipeline import run_analysis
    from orgdiff.report import build_conclusion, to_docx

    j = JOBS[job]
    jd = JOBS_DIR / job

    def persist():
        (jd / "status.json").write_text(json.dumps({"state": j["state"], "log": j["log"], "error": j["error"]}, ensure_ascii=False), encoding="utf-8")

    def say(msg, replace=False):
        if replace and j["log"] and j["log"][-1].split(" · ")[0] == msg.split(" · ")[0]:
            j["log"][-1] = msg
        else:
            j["log"].append(msg)
        persist()

    try:
        persist()
        result = run_analysis(before, after, verify=verify, golden=golden, progress=say, model=MODEL)
        say("Формирование заключения и рекомендаций")
        conc = build_conclusion(result, with_recommendations=True, model=MODEL)
        (jd / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        to_docx(conc, str(jd / "conclusion.docx"))
        (jd / "report.html").write_text(render_report(job, result, conc), encoding="utf-8")
        j.update(state="done")
        say("Готово")
    except Exception as e:  # noqa: BLE001
        from orgdiff.llm import explain_error

        j.update(state="error", error=explain_error(e) or (str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"))
        persist()


@app.get("/health")
def health():
    running = sum(1 for j in JOBS.values() if j["state"] == "running")
    return {"status": "ok", "model": MODEL, "llm": bool(os.environ.get("OPENAI_API_KEY")), "jobs_running": running}


@app.post("/analyze")
async def analyze(
    before: Annotated[list[UploadFile], File()],
    after: Annotated[list[UploadFile], File()],
    verify: Annotated[str | None, Form()] = None,
    golden: Annotated[str | None, Form()] = None,
):
    job = uuid.uuid4().hex[:10]
    jd = JOBS_DIR / job
    (jd / "before").mkdir(parents=True, exist_ok=True)
    (jd / "after").mkdir(parents=True, exist_ok=True)
    paths_b, paths_a = [], []
    for up in before:
        p = jd / "before" / Path(up.filename).name
        p.write_bytes(await up.read())
        paths_b.append(p)
    for up in after:
        p = jd / "after" / Path(up.filename).name
        p.write_bytes(await up.read())
        paths_a.append(p)
    JOBS[job] = {"state": "running", "log": ["Файлы загружены"], "error": None}
    threading.Thread(target=_worker, args=(job, paths_b, paths_a, bool(verify), bool(golden)), daemon=True).start()
    return RedirectResponse(f"/job/{job}", status_code=303)


def _status_from_disk(job: str) -> dict | None:
    p = JOBS_DIR / job / "status.json"
    if not p.exists():
        return None
    st = json.loads(p.read_text(encoding="utf-8"))
    if st["state"] == "running" and job not in JOBS:
        st["state"] = "error"
        st["error"] = "сервер был перезапущен во время анализа, запустите анализ заново"
    return st


@app.get("/job/{job}", response_class=HTMLResponse)
def job_page(job: str):
    if job not in JOBS and not (JOBS_DIR / job).exists():
        return HTMLResponse("нет такой задачи", status_code=404)
    if (JOBS_DIR / job / "report.html").exists():
        return RedirectResponse(f"/job/{job}/report", status_code=303)
    steps = "".join(
        f'<li><span class="dot">{i + 1}</span><div><div class="t">{esc(t)}</div><div class="d">{esc(d)}</div></div></li>'
        for i, (_, t, d) in enumerate(STAGES)
    )
    return JOB_PAGE.format(css=CSS, job=job, steps=steps, stages_json=json.dumps([[p, t] for p, t, _ in STAGES], ensure_ascii=False))


@app.get("/job/{job}/status")
def job_status(job: str):
    j = JOBS.get(job)
    if j:
        return {"state": j["state"], "log": j["log"], "error": j["error"]}
    st = _status_from_disk(job)
    if st:
        return st
    return JSONResponse({"state": "missing"}, status_code=404)


@app.get("/job/{job}/report", response_class=HTMLResponse)
def job_report(job: str):
    p = JOBS_DIR / job / "report.html"
    if not p.exists():
        return HTMLResponse("отчёт ещё не готов", status_code=404)
    return p.read_text(encoding="utf-8")


@app.get("/job/{job}/doc/{side}", response_class=HTMLResponse)
def job_doc(job: str, side: str):
    p = JOBS_DIR / job / "result.json"
    if side not in {"before", "after"} or not p.exists():
        return HTMLResponse("нет такого документа", status_code=404)
    return render_doc(job, side, json.loads(p.read_text(encoding="utf-8")))


@app.get("/job/{job}/conclusion.docx")
def job_docx(job: str):
    p = JOBS_DIR / job / "conclusion.docx"
    return FileResponse(p, filename="заключение.docx") if p.exists() else JSONResponse({"error": "нет файла"}, status_code=404)


@app.get("/job/{job}/result.json")
def job_json(job: str):
    p = JOBS_DIR / job / "result.json"
    return FileResponse(p, filename="result.json") if p.exists() else JSONResponse({"error": "нет файла"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("ORGDIFF_HOST", "127.0.0.1"), port=int(os.environ.get("ORGDIFF_PORT", "8765")))
