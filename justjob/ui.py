"""Page chrome shared by every dashboard page and the digest file: styles, the top bar, escaping."""
from __future__ import annotations

import html
import re
import zlib

e = html.escape

CSS = """
:root {
  --paper:#F3F5F4; --surface:#FFFFFF; --ink:#17201D; --muted:#5E6A66; --faint:#8B9692;
  --line:#DCE2DF; --line-soft:#E9EDEB; --hover:#F8FAF9;
  --jade:#0E7A61; --jade-ink:#0A5C49; --jade-wash:#E2F0EB; --amber:#A86600; --amber-wash:#FBF0DD;
  --num:"Bahnschrift","DIN Alternate","DIN 2014","Barlow",system-ui,sans-serif;
  --text:"Segoe UI Variable Text","Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
}
* { box-sizing:border-box; }
html { scroll-padding-top:90px; }
body { margin:0; background:var(--paper); color:var(--ink); font:14px/1.5 var(--text); -webkit-font-smoothing:antialiased; }
a { color:inherit; }
:focus-visible { outline:2px solid var(--jade); outline-offset:2px; border-radius:4px; }
.num, .n, .count { font-family:var(--num); font-variant-numeric:tabular-nums; }

/* top bar */
.top { position:sticky; top:0; z-index:10; background:var(--surface); border-bottom:1px solid var(--line);
       display:grid; grid-template-columns:240px auto minmax(0,1fr) auto; gap:28px; align-items:center; padding:10px 28px; }
.pages { display:flex; gap:2px; }
.pages a { padding:6px 11px; border-radius:7px; color:var(--muted); text-decoration:none; font-weight:500; }
.pages a:hover { color:var(--ink); background:var(--paper); }
.pages a.on { background:var(--jade-wash); color:var(--jade-ink); font-weight:600; }
.brand { font-family:var(--num); font-size:19px; font-weight:600; text-decoration:none; display:flex; align-items:center; gap:10px; }
.brand::before { content:""; width:12px; height:12px; border-radius:3px; background:var(--jade); }
.search { position:relative; max-width:560px; }
.search svg { position:absolute; left:12px; top:50%; translate:0 -50%; color:var(--faint); pointer-events:none; }
.search input { width:100%; padding:9px 40px 9px 36px; border:1px solid var(--line); border-radius:9px; background:var(--paper);
                font:inherit; color:var(--ink); }
.search input:focus { background:var(--surface); border-color:var(--jade); outline:none; box-shadow:0 0 0 3px var(--jade-wash); }
.search kbd { position:absolute; right:10px; top:50%; translate:0 -50%; font:12px var(--num); color:var(--faint);
              border:1px solid var(--line); border-radius:5px; padding:0 6px; background:var(--surface); }
.tabs { display:flex; gap:2px; padding:3px; background:var(--paper); border-radius:9px; }
.tabs a { padding:6px 12px; border-radius:7px; color:var(--muted); text-decoration:none; white-space:nowrap; }
.tabs a .n { margin-left:6px; color:var(--faint); }
.tabs a.on { background:var(--surface); color:var(--ink); font-weight:600; box-shadow:0 0 0 1px var(--line); }
.tabs a.on .n { color:var(--jade); }
.top .filters-btn { display:none; }
.top > *, .layout > *, .row > * { min-width:0; }   /* let grid columns shrink on small screens */

/* layout */
.layout { display:grid; grid-template-columns:240px minmax(0,1fr); gap:28px; max-width:1280px; margin:0 auto; padding:22px 28px 80px; }
.rail { position:sticky; top:78px; align-self:start; max-height:calc(100vh - 96px); overflow:auto; padding-right:6px; }
.facet { border:0; margin:0; padding:0 0 14px; }
.facet + .facet { border-top:1px solid var(--line); padding-top:14px; }
.facet legend { float:left; width:100%; padding:0; margin-bottom:6px; font-weight:600; font-size:13.5px; }
.facet legend + * { clear:both; }
.opt { display:flex; align-items:center; gap:10px; padding:5px 8px; margin:0 -8px; border-radius:7px; cursor:pointer; }
.opt:hover { background:var(--surface); }
.opt input { accent-color:var(--jade); width:15px; height:15px; margin:0; flex:none; }
.opt .n { margin-left:auto; font-size:12.5px; color:var(--faint); }
.opt.zero { color:var(--faint); }
.opt:has(input:checked) { color:var(--jade-ink); font-weight:600; }
.opt:has(input:checked) .n { color:var(--jade); }
.facet details summary { cursor:pointer; color:var(--muted); font-size:13px; padding:4px 0; list-style:none; }
.facet details summary::-webkit-details-marker { display:none; }
.facet details[open] summary { display:none; }
.rail-foot { margin-top:18px; padding-top:14px; border-top:1px solid var(--line); color:var(--muted); font-size:12.5px; }
.rail-foot p { margin:0 0 6px; }
.rail-foot .num { color:var(--ink); font-size:14px; }
.keys { color:var(--faint); }
.keys kbd { font:11.5px var(--num); border:1px solid var(--line); border-radius:4px; padding:0 4px; background:var(--surface); color:var(--muted); }

/* results */
.results-head { display:flex; align-items:flex-end; gap:16px; margin:2px 0 12px; }
.results-head .count { font-size:32px; font-weight:600; line-height:1; }
.results-head .what { color:var(--muted); padding-bottom:3px; }
.head-tools { margin-left:auto; display:flex; align-items:center; gap:8px; }
.head-tools .range { color:var(--muted); font:13px var(--num); font-variant-numeric:tabular-nums; margin-right:4px; white-space:nowrap; }
.head-tools .mini { display:inline-grid; place-items:center; width:30px; height:32px; border-radius:7px; text-decoration:none;
                    color:var(--ink); border:1px solid var(--line); background:var(--surface); }
.head-tools .mini.off { color:var(--line); }
.results-head select { padding:7px 10px; border:1px solid var(--line); border-radius:8px; background:var(--surface);
                       font:inherit; color:var(--ink); }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 14px; }
.chip { display:inline-flex; align-items:center; gap:6px; padding:3px 6px 3px 11px; border-radius:999px; background:var(--jade-wash);
        color:var(--jade-ink); font-size:12.5px; font-weight:500; text-decoration:none; }
.chip b { display:grid; place-items:center; width:17px; height:17px; border-radius:50%; font-weight:400; }
.chip:hover b { background:var(--jade); color:#fff; }
.chip.clear { background:none; color:var(--muted); text-decoration:underline; text-underline-offset:3px; padding-left:4px; }

.list { background:var(--surface); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
.row { position:relative; display:grid; grid-template-columns:42px minmax(0,1fr) 86px 112px; gap:18px; padding:16px 20px;
       border-top:1px solid var(--line-soft); transition:opacity .2s, max-height .25s; max-height:800px; }
.row:first-child { border-top:0; }
.row:hover { background:var(--hover); }
.row.cur { background:var(--hover); }
.row.cur::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--jade); }
.row.leaving { opacity:0; max-height:0; padding-top:0; padding-bottom:0; overflow:hidden; }
.mono { width:42px; height:42px; border-radius:10px; display:grid; place-items:center; font:600 15px var(--num);
        background:hsl(var(--h) 38% 92%); color:hsl(var(--h) 45% 28%); }
.title { font-size:15.5px; font-weight:600; text-decoration:none; }
.title:visited { color:var(--muted); }   /* postings you've already opened */
.title:hover { color:var(--jade-ink); text-decoration:underline; text-underline-offset:3px; }
.new { display:inline-block; margin-left:8px; padding:0 7px; border-radius:999px; background:var(--jade); color:#fff;
       font-size:11.5px; font-weight:600; vertical-align:2px; }
.meta { display:flex; flex-wrap:wrap; gap:2px 14px; margin-top:3px; color:var(--muted); font-size:13px; }
.meta .company { color:var(--ink); font-weight:500; }
.meta .remote { color:var(--jade-ink); font-weight:600; }
.meta .src { color:var(--faint); }
.why { margin:8px 0 0; color:var(--muted); font-size:13px; max-width:72ch; }
.skills { display:flex; flex-wrap:wrap; align-items:center; gap:5px; margin-top:8px; font-size:12px; }
.skills .lead { color:var(--faint); margin-right:2px; }
.skill { padding:1px 8px; border-radius:999px; border:1px solid var(--line); color:var(--ink); }
.skills .rest { color:var(--faint); }
.flag { padding:1px 8px; border-radius:999px; background:var(--amber-wash); color:var(--amber); }
.desc { margin-top:8px; }
.desc summary { cursor:pointer; color:var(--muted); font-size:13px; width:max-content; }
.desc summary:hover { color:var(--ink); }
.desc pre { white-space:pre-wrap; font:13px/1.6 var(--text); background:var(--paper); border-radius:8px; padding:12px 14px;
            max-height:440px; overflow:auto; margin:8px 0 0; max-width:80ch; }
.score { text-align:right; }
.score .num { display:block; font-size:30px; font-weight:600; line-height:1; }
.score .bar { display:block; height:3px; margin:9px 0 5px auto; width:56px; border-radius:2px; background:var(--line-soft); overflow:hidden; }
.score .bar i { display:block; height:100%; background:currentColor; }
.score .lbl { display:block; font-size:11.5px; color:var(--faint); }
.hi { color:var(--jade); } .mid { color:var(--amber); } .lo { color:var(--faint); }
.side { display:flex; flex-direction:column; gap:6px; }
.posted { font:13px var(--num); color:var(--muted); text-align:right; margin-bottom:2px; }
.posted.fresh { color:var(--jade); font-weight:600; }
.applied-on { font:600 13px var(--num); color:var(--jade-ink); text-align:right; text-decoration:none; }
a.applied-on:hover { text-decoration:underline; }
.meta .closed { color:var(--amber); font-weight:600; }
.btn { display:block; text-align:center; padding:6px 10px; border-radius:8px; border:1px solid var(--line); background:var(--surface);
       color:var(--ink); text-decoration:none; cursor:pointer; font:inherit; font-size:13px; }
.btn:hover { border-color:var(--faint); }
.btn.apply { background:var(--jade); border-color:var(--jade); color:#fff; font-weight:600; }
.btn.apply:hover { background:var(--jade-ink); }
.minor { display:flex; gap:6px; }
.minor .btn { flex:1; padding:4px 0; color:var(--muted); }
.pager { display:flex; flex-wrap:wrap; justify-content:center; align-items:center; gap:4px; margin:20px 0 6px; }
.pager a, .pager span { min-width:36px; height:36px; padding:0 10px; display:inline-grid; place-items:center; border-radius:8px;
                        font:14px var(--num); font-variant-numeric:tabular-nums; text-decoration:none; color:var(--ink); }
.pager a:hover { background:var(--surface); box-shadow:0 0 0 1px var(--line); }
.pager .cur { background:var(--ink); color:#fff; font-weight:600; }
.pager .gap { min-width:18px; padding:0; color:var(--faint); }
.pager .step { font:14px var(--text); color:var(--muted); }
.pager .step.off { color:var(--line); }
.showing { text-align:center; color:var(--faint); font-size:12.5px; }
.empty { padding:64px 24px; text-align:center; color:var(--muted); }
.empty strong { display:block; color:var(--ink); font-size:16px; margin-bottom:4px; }
.toast { position:fixed; left:50%; bottom:24px; translate:-50% 0; display:flex; gap:14px; align-items:center; padding:10px 12px 10px 16px;
         border-radius:10px; background:var(--ink); color:#fff; font-size:13.5px; opacity:0; pointer-events:none; transition:opacity .2s; }
.toast.on { opacity:1; pointer-events:auto; }
.toast button { background:none; border:0; color:#9FE3CF; font:inherit; font-weight:600; cursor:pointer; padding:2px 4px; }

/* sign-in page */
.login-page { display:grid; place-items:center; min-height:100vh; padding:16px; }
.login { width:min(360px, 100%); background:var(--surface); border:1px solid var(--line); border-radius:14px;
         padding:28px; display:grid; gap:10px; }
.login .brand { margin-bottom:12px; }
.login label { font-weight:600; font-size:13.5px; }
.login input { padding:10px 12px; border:1px solid var(--line); border-radius:9px; font:inherit; color:var(--ink);
               background:var(--paper); }
.login input:focus { outline:none; border-color:var(--jade); box-shadow:0 0 0 3px var(--jade-wash); background:var(--surface); }
.login .btn { padding:10px; font-size:14px; margin-top:6px; }
.login-error { margin:0; color:#B42318; font-size:13px; }

/* profile page */
.page-head { display:flex; align-items:flex-end; flex-wrap:wrap; gap:6px 14px; margin:2px 0 14px; }
.page-head h1 { margin:0; font:600 30px/1 var(--num); }
.page-head p { margin:0; color:var(--muted); padding-bottom:2px; }
.notice { padding:10px 14px; border-radius:10px; background:var(--jade-wash); color:var(--jade-ink); margin:0 0 14px; font-weight:500; }
.notice.warn { background:var(--amber-wash); color:var(--amber); }
.card { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:20px 22px; margin-bottom:14px;
        scroll-margin-top:90px; }
.card h2 { margin:0; font:600 17px var(--num); }
.card .blurb { margin:3px 0 16px; color:var(--muted); font-size:13px; max-width:72ch; }
.fields { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:14px 18px; }
.field { display:grid; gap:5px; align-content:start; }
.field.wide { grid-column:1 / -1; }
.field label { font-weight:600; font-size:13px; }
.field .req { color:var(--jade); margin-left:3px; }
.field input, .field select, .field textarea { width:100%; padding:8px 11px; border:1px solid var(--line); border-radius:8px;
                                               font:inherit; color:var(--ink); background:var(--surface); }
.field textarea { min-height:78px; resize:vertical; }
.field :is(input, select, textarea):focus { outline:none; border-color:var(--jade); box-shadow:0 0 0 3px var(--jade-wash); }
.field.missing :is(input, select, textarea) { border-color:#E3C48F; background:#FFFCF6; }
.field .help { color:var(--faint); font-size:12px; }
.savebar { position:sticky; bottom:0; z-index:5; display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; padding:14px 0 18px;
           background:linear-gradient(to bottom, transparent, var(--paper) 35%); }
.savebar .btn { padding:9px 20px; font-size:14px; }
.savebar span { color:var(--muted); font-size:12.5px; }
.savebar span.dirty { color:var(--amber); font-weight:600; }
.ready { padding-bottom:14px; margin-bottom:12px; border-bottom:1px solid var(--line); }
.ready .label { font-weight:600; font-size:13.5px; }
.ready .of { font:600 26px/1.1 var(--num); margin-top:4px; }
.ready .of small { font-size:14px; color:var(--faint); font-weight:400; }
.meter { height:6px; border-radius:3px; background:var(--line-soft); overflow:hidden; margin:8px 0; }
.meter i { display:block; height:100%; background:var(--jade); }
.ready ul { margin:8px 0 0; padding:0; list-style:none; font-size:12.5px; }
.ready li a { color:var(--amber); text-decoration:none; }
.ready li a:hover { text-decoration:underline; }
.ready p { margin:6px 0 0; color:var(--muted); font-size:12.5px; }
.ready-line { display:none; margin:0 0 14px; }
.ready-line .meter { margin:0 0 6px; }
.ready-line p { margin:0; color:var(--muted); font-size:13px; }
.ready-line b { color:var(--ink); }
.field ::placeholder { color:#A3ADA9; }
.toc a { display:block; padding:5px 8px; margin:0 -8px; border-radius:7px; color:var(--muted); text-decoration:none; }
.toc a:hover { background:var(--surface); color:var(--ink); }
.file { display:flex; align-items:center; gap:14px; padding:12px 14px; border:1px dashed var(--line); border-radius:10px;
        background:var(--paper); }
.file .doc { flex:none; width:38px; height:46px; border-radius:6px; display:grid; place-items:center;
             background:var(--jade-wash); color:var(--jade-ink); font:600 11px var(--num); }
.file .name { font-weight:600; overflow-wrap:anywhere; }
.file .sub { color:var(--muted); font-size:12.5px; }
.file .actions { margin-left:auto; display:flex; flex-wrap:wrap; gap:8px; }
.file .actions .btn { display:inline-block; }
.status-line { margin:10px 0 0; color:var(--muted); font-size:13px; min-height:1.5em; }
.status-line.err { color:#B42318; }
.facts { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:4px 28px; margin-top:18px; }
.facts section { min-width:0; }
.facts section.wide { grid-column:1 / -1; }
.facts h3 { margin:0 0 4px; font-size:12px; letter-spacing:.04em; text-transform:uppercase; color:var(--faint); font-weight:600; }
.fact { padding:9px 0; border-top:1px solid var(--line-soft); }
.fact .what { font-weight:600; }
.fact .where { color:var(--muted); }
.fact .when { color:var(--faint); font:12.5px var(--num); }
.fact ul { margin:4px 0 0; padding-left:18px; color:var(--muted); font-size:13px; }
.facts .skills { margin-top:4px; }
@media (max-width:960px) { .facts { grid-template-columns:1fr; } }

/* apply review page */
.btn.autofill { color:var(--jade-ink); border-color:#9CCDBE; font-weight:600; }
.btn.autofill:hover { border-color:var(--jade); background:var(--jade-wash); }
.apply-wrap { max-width:960px; margin:0 auto; padding:22px 28px 40px; }
.back { color:var(--muted); text-decoration:none; font-size:13px; }
.back:hover { color:var(--ink); }
.job-head { display:flex; gap:16px; align-items:flex-start; margin:10px 0 16px; }
.job-head h1 { margin:0; font:600 26px/1.2 var(--num); }
.job-head .links { margin-left:auto; flex:none; }
.tally { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 14px; }
.tally span { padding:4px 11px; border-radius:999px; background:var(--surface); border:1px solid var(--line); font-size:13px;
              color:var(--muted); }
.tally b { font-family:var(--num); color:var(--ink); margin-right:2px; }
.tally .need { background:var(--amber-wash); border-color:#EBD3A5; color:var(--amber); }
.tally .need b { color:var(--amber); }
.qa { display:grid; grid-template-columns:minmax(0,5fr) minmax(0,7fr); gap:6px 22px; padding:14px 0;
      border-top:1px solid var(--line-soft); }
.card h2 + .qa { border-top:0; margin-top:6px; }
.qa .q { font-weight:600; font-size:13.5px; }
.qa .req { color:var(--jade); margin-left:3px; }
.qa .desc { color:var(--faint); font-size:12px; margin-top:3px; }
.qa .control { display:grid; gap:7px; align-content:start; min-width:0; }
.qa :is(input, select, textarea) { width:100%; padding:8px 11px; border:1px solid var(--line); border-radius:8px; font:inherit;
                                   color:var(--ink); background:var(--surface); }
.qa textarea { min-height:120px; resize:vertical; }
.qa :is(input, select, textarea):focus { outline:none; border-color:var(--jade); box-shadow:0 0 0 3px var(--jade-wash); }
.qa.need { margin:0 -22px; padding:14px 22px; background:linear-gradient(90deg, #FDF6EA, var(--surface) 70%); }
.qa .why { display:flex; flex-wrap:wrap; gap:4px 8px; align-items:center; font-size:12px; color:var(--muted); }
.origin { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11.5px; font-weight:600; background:var(--paper);
       color:var(--muted); }
.origin.profile, .origin.rule, .origin.memory { background:var(--jade-wash); color:var(--jade-ink); }
.origin.ai { background:#ECEAF8; color:#4B3FA0; }
.origin.you { background:var(--ink); color:#fff; }
.needs { color:var(--amber); font-weight:600; }
.checks { display:grid; gap:5px; }
.checks label { display:flex; gap:8px; align-items:center; }
.checks input { width:auto; accent-color:var(--jade); }
.file-note { padding:8px 11px; border-radius:8px; background:var(--paper); color:var(--muted); font-size:13px; }
.drafting { display:flex; gap:16px; align-items:center; }
.drafting p, #draft-error p { margin:2px 0 0; color:var(--muted); }
#draft-error .btn { display:inline-block; margin-top:12px; }
.spinner { flex:none; width:22px; height:22px; border-radius:50%; border:3px solid var(--jade-wash); border-top-color:var(--jade);
           animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
/* applications board */
.track-wrap { max-width:1440px; margin:0 auto; padding:22px 28px 40px; }
.row-head { display:flex; align-items:flex-start; gap:16px; }
.row-head > a.btn { margin-left:auto; flex:none; display:inline-block; margin-top:8px; }
.add-app summary { cursor:pointer; font-weight:600; color:var(--jade-ink); }
.add-app[open] summary { margin-bottom:14px; }
.add-app .fields { grid-template-columns:repeat(3, minmax(0,1fr)); }
.add-actions { display:flex; align-items:flex-end; }
.add-actions .btn { padding:9px 20px; }
.board { display:grid; grid-template-columns:repeat(5, minmax(220px, 1fr)); gap:14px; align-items:start; overflow-x:auto;
         padding-bottom:8px; }
.col { background:var(--paper); border:1px solid var(--line); border-radius:12px; padding:10px; min-height:160px; }
.col header { display:flex; align-items:center; gap:8px; padding:2px 4px 10px; }
.col h2 { margin:0; font:600 14px var(--num); }
.col .n { margin-left:auto; font:600 12.5px var(--num); color:var(--muted); background:var(--surface); border:1px solid var(--line);
          border-radius:999px; padding:0 8px; }
.col.over, .zone.over { outline:2px dashed var(--jade); outline-offset:-2px; background:var(--jade-wash); }
.cards { display:grid; gap:8px; }
.zones { display:flex; flex-wrap:wrap; gap:5px; margin:0 0 10px; }
.zone { font-size:11.5px; padding:3px 8px; border:1px dashed var(--line); border-radius:999px; color:var(--muted);
        background:var(--surface); }
.tcard { display:grid; grid-template-columns:34px minmax(0,1fr); gap:10px; background:var(--surface); border:1px solid var(--line);
         border-radius:10px; padding:10px; cursor:grab; }
.tcard:hover { border-color:var(--faint); }
.tcard.dragging { opacity:.5; }
.tcard .mono { width:34px; height:34px; border-radius:8px; font-size:13px; }
.tbody { display:grid; gap:3px; min-width:0; }
.tcard .t { font-weight:600; font-size:13.5px; line-height:1.3; text-decoration:none; color:var(--ink); }
.tcard .t:hover { color:var(--jade-ink); text-decoration:underline; }
.tcard .co { color:var(--muted); font-size:12.5px; }
.tmeta { display:flex; flex-wrap:wrap; gap:2px 10px; color:var(--faint); font-size:12px; }
.chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:2px; }
.chip { font-size:11.5px; font-weight:600; padding:1px 8px; border-radius:999px; background:var(--paper); color:var(--muted); }
.chip.warn { background:var(--amber-wash); color:var(--amber); }
.chip.end.accepted { background:var(--jade-wash); color:var(--jade-ink); }
.tcard select.move { margin-top:6px; width:100%; padding:4px 8px; border:1px solid var(--line); border-radius:7px; font:inherit;
                     font-size:12.5px; color:var(--ink); background:var(--surface); }
.linkish { border:0; background:none; padding:2px 0 0; color:var(--jade-ink); font:inherit; font-size:12px; cursor:pointer;
           text-decoration:underline; }
.empty { color:var(--faint); font-size:12.5px; margin:4px; }
/* one application */
.job-head .links { display:flex; gap:8px; }
.job-head .links .btn { display:inline-block; }
.stepper { display:flex; list-style:none; margin:16px 0 18px; padding:0; }
.stepper li { flex:1; display:grid; justify-items:start; gap:2px; position:relative; padding-top:18px; }
.stepper li::before { content:""; position:absolute; top:6px; left:0; right:0; height:2px; background:var(--line); }
.stepper li.done::before, .stepper li.now::before { background:var(--jade); }
.stepper .dot { position:absolute; top:0; left:0; width:14px; height:14px; border-radius:50%; background:var(--surface);
                border:2px solid var(--line); }
.stepper li.done .dot { background:var(--jade); border-color:var(--jade); }
.stepper li.now .dot { background:var(--surface); border-color:var(--jade); box-shadow:0 0 0 3px var(--jade-wash); }
.stepper .l { font-weight:600; font-size:13px; }
.stepper li.todo .l { color:var(--faint); font-weight:500; }
.stepper .d { color:var(--muted); font-size:12px; }
.moves { display:grid; gap:10px; }
.moves .group { display:flex; flex-wrap:wrap; align-items:center; gap:6px; }
.moves .glabel { color:var(--muted); font-size:12.5px; width:64px; }
.moves .btn { display:inline-block; padding:5px 12px; }
.moves .btn.on { background:var(--jade-wash); border-color:#9CCDBE; color:var(--jade-ink); font-weight:600; opacity:1; }
.note-in, .add-note textarea { width:100%; padding:8px 11px; border:1px solid var(--line); border-radius:8px; font:inherit;
                               color:var(--ink); background:var(--surface); }
.add-note { display:grid; gap:8px; justify-items:start; margin:12px 0 16px; }
.add-note .btn { padding:7px 16px; }
.timeline { list-style:none; margin:0; padding:0; }
.ev { display:grid; grid-template-columns:110px minmax(0,1fr); gap:12px; padding:10px 0; border-top:1px solid var(--line-soft); }
.ev .when { color:var(--muted); font-size:12.5px; }
.ev p { margin:0; white-space:pre-wrap; }
.ev.stage b { font-weight:600; }
.ev.stage p { margin-top:3px; color:var(--muted); }
/* insights */
.apply-wrap.wide { max-width:1120px; }
.range { display:flex; gap:2px; padding:3px; background:var(--surface); border:1px solid var(--line); border-radius:9px;
         width:max-content; margin:0 0 16px; }
.range a { padding:6px 12px; border-radius:7px; color:var(--muted); text-decoration:none; font-size:13px; }
.range a.on { background:var(--jade-wash); color:var(--jade-ink); font-weight:600; }
.tiles { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:12px; margin-bottom:14px; }
.tile { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.tile .tl { color:var(--muted); font-size:12.5px; }
.tile .tv { font:600 28px/1.2 var(--text); margin-top:4px; color:var(--ink); }
.tile .ts { color:var(--faint); font-size:12px; margin-top:2px; }
.funnel { display:grid; gap:10px; margin-top:6px; }
.frow { display:grid; grid-template-columns:96px minmax(0,1fr); gap:12px; align-items:center; }
.fname { font-weight:600; font-size:13px; }
.ftrack { display:flex; align-items:center; gap:10px; min-width:0; border-left:1px solid var(--line); }
.fbar { height:20px; background:var(--jade); border-radius:0 4px 4px 0; min-width:2px; flex:none;
        width:calc((100% - 210px) * var(--w)); }   /* room left at the tip for the value */
.fbar:focus-visible, .mtrack:focus-visible, .wcol:focus-visible { outline:2px solid var(--ink); outline-offset:2px; }
.fval { font:600 13px var(--text); white-space:nowrap; }
.muted { color:var(--muted); font-weight:400; }
.foot { margin:12px 0 0; }
.two { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:0 14px; }
.meters { display:grid; gap:9px; }
.mrow { display:grid; grid-template-columns:minmax(0,150px) minmax(0,1fr) 112px; gap:12px; align-items:center; font-size:13px; }
.mname { overflow-wrap:anywhere; }
.mtrack { height:8px; border-radius:999px; background:var(--jade-wash); }
.mfill { height:100%; border-radius:999px; background:var(--jade); }
.mval { font-weight:600; white-space:nowrap; text-align:right; font-variant-numeric:tabular-nums; }
.weekly { display:grid; grid-template-columns:28px minmax(0,1fr); gap:8px; margin-top:6px; }
.wy { display:flex; flex-direction:column; justify-content:space-between; color:var(--faint); font-size:11.5px;
      font-variant-numeric:tabular-nums; text-align:right; height:160px; }
.wcols { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(0,1fr); gap:2px; }
.wcol { display:grid; grid-template-rows:160px auto; outline:none; }
.wplot { display:flex; flex-direction:column; justify-content:flex-end; align-items:center; border-bottom:1px solid var(--line);
         border-top:1px solid var(--line-soft); }
.wbar { width:min(24px, 70%); background:var(--jade); border-radius:4px 4px 0 0; }
.wcol:hover .wbar { background:var(--jade-ink); }
.cv { font:600 12px var(--text); color:var(--ink); margin-bottom:3px; }
.wx { color:var(--faint); font-size:11px; text-align:center; padding-top:4px; white-space:nowrap; }
.as-table { margin-top:12px; font-size:13px; }
.as-table summary { cursor:pointer; color:var(--muted); font-size:12.5px; }
.sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); }
.tip { position:absolute; z-index:20; background:var(--ink); color:#fff; font-size:12.5px; padding:6px 10px; border-radius:8px;
       pointer-events:none; max-width:280px; box-shadow:0 6px 20px rgba(0,0,0,.18); }
/* answers page */
.page-head h1 { margin:6px 0 4px; font:600 26px/1.2 var(--num); }
.page-head p { margin:0 0 16px; color:var(--muted); max-width:72ch; }
.history { width:100%; border-collapse:collapse; font-size:13.5px; }
.history th { text-align:left; font-weight:600; color:var(--muted); font-size:12px; padding:6px 10px 6px 0; }
.history td { padding:8px 10px 8px 0; border-top:1px solid var(--line-soft); vertical-align:top; }
.history .num { text-align:right; white-space:nowrap; }
.saved .actions { display:flex; gap:8px; }
.saved .actions .btn { display:inline-block; padding:5px 14px; }
.btn.danger { color:#A23B2A; }
.btn.danger:hover { border-color:#A23B2A; }
.live { border-left:4px solid var(--jade); }
.live[data-state="waiting"] { border-left-color:var(--amber); }
.live[data-state="failed"], .live[data-state="closed"] { border-left-color:var(--faint); }
.live .msg { margin:0; font-weight:600; }
.live ul { margin:8px 0 0; padding-left:18px; color:var(--muted); font-size:13px; }
.live ul:empty { display:none; }
.savebar .btn { display:inline-block; }

/* digest file */
.digest main { max-width:900px; margin:0 auto; padding:22px 28px 60px; }
.digest h2 { font:600 18px var(--num); margin:26px 0 10px; }
.digest .top { grid-template-columns:1fr; }
.meta-line { color:var(--muted); }

@media (max-width:960px) {
  .top { grid-template-columns:auto 1fr auto; gap:12px; padding:10px 16px; }
  .pages { grid-column:2; grid-row:1; overflow-x:auto; scrollbar-width:none; }
  .pages a { padding:6px 8px; white-space:nowrap; }
  .ftrack { flex-wrap:wrap; row-gap:3px; }
  .fbar { width:calc(100% * var(--w)); }
  .fval { flex-basis:100%; }
  .search { grid-column:1 / -1; grid-row:2; max-width:none; }
  .tabs { grid-column:1 / -1; grid-row:3; overflow:auto; }
  .top .filters-btn { display:inline-block; grid-column:3; grid-row:1; }
  .fields { grid-template-columns:1fr; }
  .qa { grid-template-columns:1fr; }
  .qa.need { margin:0 -16px; padding:14px 16px; }
  .apply-wrap { padding:16px; }
  .job-head { flex-wrap:wrap; }
  .job-head .links { margin-left:0; }
  .profile .rail { display:none; }
  .track-wrap { padding:16px; }
  .board { grid-template-columns:repeat(5, minmax(240px, 82vw)); }
  .add-app .fields, .two { grid-template-columns:1fr; }
  .tiles { grid-template-columns:repeat(2, minmax(0,1fr)); }
  .mrow { grid-template-columns:minmax(0,1fr) 100px; }
  .mrow .mtrack { grid-column:1 / -1; grid-row:2; }
  .ev { grid-template-columns:1fr; gap:2px; }
  .stepper .d { display:none; }
  .ready-line { display:block; }
  .head-tools { flex-wrap:wrap; }
  .head-tools select { flex:1 1 130px; min-width:0; }
  .file { flex-wrap:wrap; }
  .file .actions { margin-left:0; width:100%; }
  .layout { grid-template-columns:1fr; padding:16px; }
  .rail { display:none; position:static; max-height:none; background:var(--surface); border:1px solid var(--line);
          border-radius:12px; padding:14px 16px; }
  body.show-filters .rail { display:block; }
  .row { grid-template-columns:38px minmax(0,1fr) 64px; gap:12px; padding:14px; }
  .side { grid-column:2 / -1; flex-direction:row; align-items:center; flex-wrap:wrap; }
  .side .apply { flex:1; }
  .minor { flex:none; }
  .minor .btn { padding:4px 8px; }
  .results-head .count { font-size:26px; }
  .results-head { flex-wrap:wrap; }
  .head-tools { margin-left:0; width:100%; }
  .head-tools .range { margin-right:auto; }
  .posted { text-align:left; margin:0 auto 0 0; }
  .applied-on { text-align:left; }
  .score .num { font-size:24px; }
}
@media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
"""

# The dashboard's pages, in top-bar order: (key, label, path).
PAGES = (("jobs", "Jobs", "/"), ("tracker", "Applications", "/tracker"), ("insights", "Insights", "/insights"),
         ("profile", "Profile", "/profile"), ("answers", "Answers", "/answers"))


def top_bar(active: str, middle: str = "", right: str = "") -> str:
    """The sticky header: brand, page links, then whatever the page puts in the middle and on the right."""
    links = "".join(
        f'<a class="on" href="{path}" aria-current="page">{label}</a>' if key == active else f'<a href="{path}">{label}</a>'
        for key, label, path in PAGES)
    return (f'<header class="top"><a class="brand" href="/">Just Job</a>'
            f'<nav class="pages" aria-label="Pages">{links}</nav>{middle}{right}</header>')


def monogram(company: str) -> tuple[str, int]:
    """Two letters and a stable hue per company, so the same employer is easy to spot."""
    words = re.findall(r"[A-Za-z0-9]+", company) or ["?"]
    letters = (words[0][0] + (words[1][0] if len(words) > 1 else words[0][1:2])).upper()
    return letters, zlib.crc32(company.lower().encode()) % 360
