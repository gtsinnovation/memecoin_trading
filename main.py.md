# **main.py**

*`# main.py`*  
`import asyncio`  
`import json`  
`import random`  
`import logging`  
`import sys`  
`import traceback`  
`from datetime import datetime`  
`from typing import List`  
`from fastapi import FastAPI, WebSocket, WebSocketDisconnect`  
`from fastapi.responses import HTMLResponse`  
`import asyncpg`

*`# Diagnostic block to print detailed local code tracebacks if an import fails`*  
`try:`  
    `from engine import agent_network, DB_DSN`  
`except Exception as import_error:`  
    `print("\n" + "!" * 50)`  
    `print("CRITICAL IMPORT EXCEPTION DETECTED IN YOUR LOCAL PROJECT FILES:")`

    `traceback.print_exc(file=sys.stdout)`  
    `print("!" * 50 + "\n")`  
    `raise import_error`

`logging.basicConfig(level=logging.INFO)`  
`logger = logging.getLogger("main_server")`

`app = FastAPI(title="Integrated Production Analytics and Telemetry Control Node")`

`class WebSocketConnectionManager:`  
    `"""Orchestrates active client subscriber socket pools to broadcast system telemetry updates."""`  
    `def __init__(self):`  
        `self.active_connections: List[WebSocket] = []`

    `async def connect(self, websocket: WebSocket):`  
        `await websocket.accept()`  
        `self.active_connections.append(websocket)`

    `def disconnect(self, websocket: WebSocket):`

        `self.active_connections.remove(websocket)`

    `async def broadcast(self, message: dict):`  
        `payload = json.dumps(message)`  
        `for connection in self.active_connections:`  
            `try:`  
                `await connection.send_text(payload)`  
            `except Exception:`  
                `pass`

`ws_manager = WebSocketConnectionManager()`

`async def pipeline_executor_worker():`  
    `"""`  
    `Continuous background loop that seeds simulation context data, executes`   
    `the core agent network, and broadcasts metrics payloads to chart endpoints.`  
    `"""`  
    `await asyncio.sleep(4.0)`  
    `tokens = ["MOONCAT", "PUMPIT", "SOLAMA", "ORBITX", "DOGEVERSE"]`

      
    `while True:`  
        `try:`  
            `pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)`  
            `if not pool:`  
                `await asyncio.sleep(2.0)`  
                `continue`  
                  
            `async with pool.acquire() as conn:`  
                `totals = await conn.fetchrow("""`  
                    `SELECT COUNT(*)::int as total,`  
                           `COUNT(*) FILTER (WHERE session_status = 'APPROVED')::int as approved,`  
                           `COUNT(*) FILTER (WHERE session_status = 'REJECTED')::int as rejected`  
                    `FROM trading_sessions;`  
                `""")`  
                `funds = await conn.fetchval("SELECT COALESCE(SUM(allocated_usd), 0.0)::float FROM active_positions;")`  
                `positions = await conn.fetch("SELECT token_symbol, allocated_usd, entry_trigger FROM active_positions ORDER BY id DESC LIMIT 4;")`  
                `funnel_counts = await conn.fetch("""`  
                    `SELECT final_briefing, COUNT(*)::int as count` 

                    `FROM trading_sessions`   
                    `WHERE session_status = 'REJECTED'`   
                    `GROUP BY final_briefing;`  
                `""")`  
                `alert_rows = await conn.fetch("SELECT agent_name, message FROM system_alerts WHERE log_level IN ('WARN', 'ERROR', 'CRITICAL') ORDER BY id DESC LIMIT 3;")`  
            `await pool.close()`

            `funnel_data = {"B_SENTINEL": 0, "E_SIGNAL": 0, "F_ATLAS": 0, "G_ANCHOR": 0}`  
            `for row in funnel_counts:`  
                `brief = row["final_briefing"] or ""`  
                `if "B_SENTINEL" in brief: funnel_data["B_SENTINEL"] += row["count"]`  
                `elif "E_SIGNAL" in brief: funnel_data["E_SIGNAL"] += row["count"]`  
                `elif "F_ATLAS" in brief: funnel_data["F_ATLAS"] += row["count"]`  
                `elif "G_ANCHOR" in brief: funnel_data["G_ANCHOR"] += row["count"]`

            `target_token = random.choice(tokens)`  
            `price_sim = round(random.uniform(0.005, 0.65), 5)`  
            `liq_sim = random.choice([25000.0, 38000.0, 85000.0, 190000.0])`   
            `social_sim = random.uniform(20.0, 100.0)`

            `flow_sim = random.choice([5.0, 30.0, 75.0])`   
            `concentration_sim = random.choice([18.5, 24.0, 35.5])`   
            `slippage_sim = random.choice([1.2, 1.8, 3.4])` 

            `inputs = {`  
                `"token_symbol": target_token,`  
                `"token_address": f"0x{random.randint(1000, 9999)}...pump",`  
                `"current_price": price_sim,`  
                `"pool_liquidity_usd": liq_sim,`  
                `"social_volume_score": social_sim,`  
                `"onchain_flow_velocity": flow_sim,`  
                `"top_10_holder_percentage": concentration_sim,`  
                `"estimated_slippage_percent": slippage_sim,`  
                `"onchain_volume_increasing": True`  
            `}`

            `loop = asyncio.get_event_loop()`  
            `final_state = await loop.run_in_executor(None, lambda: agent_network.invoke(inputs))`  
            

            `log_msg = final_state.get("termination_reason") or final_state.get("final_briefing_compiled")`  
            `agents = ["A_ORBIT", "B_SENTINEL", "C_VECTOR", "D_PULSE", "E_SIGNAL", "F_ATLAS", "G_ANCHOR", "H_FUSE", "I_ACCOUNTANT", "Z_CLOSER"]`  
              
            `broadcast_payload = {`  
                `"timestamp": datetime.now().strftime("%H:%M:%S"),`  
                `"summary": {`  
                    `"total_sessions": (totals["total"] if totals else 0) + 1,`  
                    `"approved_sessions": totals["approved"] if totals else 0,`  
                    `"rejected_sessions": totals["rejected"] if totals else 0,`  
                    `"total_capital": funds or 0.0`  
                `},`  
                `"latest_log": f"[{target_token}] {log_msg}",`  
                `"funnel_rejections": funnel_data,`  
                `"latencies": {ag: round(random.uniform(10.0, 55.0), 1) for ag in agents},`  
                `"divergence": {`  
                    `"social_velocity": social_sim,`  
                    `"onchain_flow": flow_sim`  
                `},`  
                `"invalidation_proximity": round(random.uniform(10.0, 100.0), 1),`

                `"active_positions": [dict(r) for r in positions],`  
                `"alerts": [dict(a) for a in alert_rows]`  
            `}`

            `await ws_manager.broadcast(broadcast_payload)`  
        `except Exception as e:`  
            `logger.error(f"Error handling network execution loops: {e}")`  
              
        `await asyncio.sleep(3.0)`

`@app.on_event("startup")`  
`def start_pipeline_loops():`  
    `asyncio.create_task(pipeline_executor_worker())`

`@app.websocket("/ws/metrics")`  
`async def websocket_route(websocket: WebSocket):`  
    `await ws_manager.connect(websocket)`  
    `try:`  
        `while True:`

            `await websocket.receive_text()`  
    `except WebSocketDisconnect:`  
        `ws_manager.disconnect(websocket)`

`@app.get("/", response_class=HTMLResponse)`  
`async def get_dashboard_interface():`  
    `html_content = """`  
    `<!DOCTYPE html>`  
    `<html lang="en">`  
    `<head>`  
        `<meta charset="UTF-8">`  
        `<meta name="viewport" content="width=device-width, initial-scale=1.0">`  
        `<title>Production Network Control Panel</title>`  
        `<script src="https://cdn.tailwindcss.com"></script>`  
        `<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>`  
        `<style>`  
            `@keyframes pulse-glow { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.05); opacity: 0.7; } }`  
            `.active-glow { animation: pulse-glow 2s infinite ease-in-out; }`  
            `.chart-frame { position: relative; width: 100%; height: 180px; }`

        `</style>`  
    `</head>`  
    `<body class="bg-slate-950 text-slate-100 font-sans min-h-screen">`  
        `<header class="border-b border-slate-800 bg-slate-900/40 backdrop-blur px-6 py-4 sticky top-0 z-50">`  
            `<div class="max-w-7xl mx-auto flex justify-between items-center">`  
                `<div>`  
                    `<h1 class="text-sm font-bold text-white flex items-center gap-2">`  
                        `<span class="h-2 w-2 rounded-full bg-emerald-400 active-glow"></span>`  
                        `10-Agent Production Network Matrix`  
                    `</h1>`  
                `</div>`  
                `<div id="ws-badge" class="px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/20">`  
                    `DISCONNECTED`  
                `</div>`  
            `</div>`  
        `</header>`

        `<main class="max-w-7xl mx-auto p-6 space-y-6">`  
            `<section class="grid grid-cols-2 lg:grid-cols-4 gap-4">`

                `<div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">`  
                    `<p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Total Pipelines</p>`  
                    `<p id="stat-total" class="text-xl font-bold mt-1">--</p>`  
                `</div>`  
                `<div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">`  
                    `<p class="text-[10px] font-bold text-emerald-400 uppercase tracking-wider">Approved Transits</p>`  
                    `<p id="stat-approved" class="text-xl font-bold text-emerald-400 mt-1">--</p>`  
                `</div>`  
                `<div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">`  
                    `<p class="text-[10px] font-bold text-rose-400 uppercase tracking-wider">Gating Rejections</p>`  
                    `<p id="stat-rejected" class="text-xl font-bold text-rose-400 mt-1">--</p>`  
                `</div>`  
                `<div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">`  
                    `<p class="text-[10px] font-bold text-indigo-400 uppercase tracking-wider">Ledger Allocations</p>`  
                    `<p id="stat-capital" class="text-xl font-bold text-indigo-400 mt-1">$--</p>`  
                `</div>`  
            `</section>`

            `<section class="grid grid-cols-1 lg:grid-cols-2 gap-6">`

                `<div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">`  
                    `<h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Parameter Rejection Funnel</h3>`  
                    `<div class="chart-frame flex-1"><canvas id="chart-funnel"></canvas></div>`  
                `</div>`

                `<div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">`  
                    `<h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Agent Latency Distribution (ms)</h3>`  
                    `<div class="chart-frame flex-1"><canvas id="chart-latency"></canvas></div>`  
                `</div>`

                `<div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">`  
                    `<h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Hype Velocity vs Onchain Inflows</h3>`  
                    `<div class="chart-frame flex-1"><canvas id="chart-divergence"></canvas></div>`  
                `</div>`

                `<div class="bg-slate-900 border border-slate-800 p-5 rounded-xl flex flex-col">`  
                    `<h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Accounting Ledger Holdings</h3>`  
                    `<div id="positions-box" class="flex-1 space-y-2 text-xs overflow-y-auto pt-1">`  
                        `<p class="text-slate-500 italic">Syncing asset data tables...</p>`

                    `</div>`  
                `</div>`  
            `</section>`

            `<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">`  
                `<section class="lg:col-span-2 bg-slate-900 border border-slate-800 p-5 rounded-xl">`  
                    `<h3 class="text-xs font-bold text-slate-300 uppercase mb-2">Telemetry Trace Console</h3>`  
                    `<div id="console-log" class="bg-slate-950 p-4 font-mono text-[11px] h-28 overflow-y-auto space-y-1 rounded-lg border border-slate-800">`  
                        `<div class="text-slate-500">// Processing metrics feeds...</div>`  
                    `</div>`  
                `</section>`  
                  
                `<section class="bg-slate-900 border border-slate-800 p-5 rounded-xl">`  
                    `<h3 class="text-xs font-bold text-rose-400 uppercase mb-2">Framework Alerts Outbox Buffer</h3>`  
                    `<div id="alerts-box" class="space-y-1.5 h-28 overflow-y-auto text-[10px] font-mono">`  
                        `<p class="text-slate-500 italic">No warnings active.</p>`  
                    `</div>`  
                `</section>`  
            `</div>`

        `</main>`

        `<script>`  
            `let cFunnel, cLatency, cDivergence;`  
            `const labelsBuffer = [];`  
            `const streamSocial = [];`  
            `const streamFlow = [];`

            `function initCharts() {`  
                `cFunnel = new Chart(document.getElementById('chart-funnel').getContext('2d'), {`  
                    `type: 'bar',`  
                    `data: {`  
                        `labels: ['B_SENTINEL (Liquidity)', 'E_SIGNAL (Hype)', 'F_ATLAS (Concentration)', 'G_ANCHOR (Slippage)'],`  
                        `datasets: [{ data: [0, 0, 0, 0], backgroundColor: ['#f43f5e', '#f59e0b', '#ec4899', '#6366f1'] }]`  
                    `},`  
                    `options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }`  
                `});`

                `cLatency = new Chart(document.getElementById('chart-latency').getContext('2d'), {`

                    `type: 'bar',`  
                    `data: {`  
                        `labels: ['ORB', 'SNT', 'VEC', 'PLS', 'SIG', 'ATL', 'ANC', 'FUS', 'ACC', 'CLS'],`  
                        `datasets: [{ data: Array(10).fill(0), backgroundColor: '#10b981' }]`  
                    `},`  
                    `options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }`  
                `});`

                `cDivergence = new Chart(document.getElementById('chart-divergence').getContext('2d'), {`  
                    `type: 'line',`  
                    `data: {`  
                        `labels: labelsBuffer,`  
                        `datasets: [`  
                            `{ label: 'Social Momentum', data: streamSocial, borderColor: '#f59e0b', tension: 0.15 },`  
                            `{ label: 'Onchain Flow', data: streamFlow, borderColor: '#06b6d4', tension: 0.15 }`  
                        `]`  
                    `},`  
                    `options: { responsive: true, maintainAspectRatio: false }`  
                `});`

            `}`

            `function initWS() {`  
                `const badge = document.getElementById('ws-badge');`  
                `const consoleLog = document.getElementById('console-log');`  
                `const alertsBox = document.getElementById('alerts-box');`  
                `const positionsBox = document.getElementById('positions-box');`

                `const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';`  
                ``const socket = new WebSocket(`${protocol}//${window.location.host}/ws/metrics`);``

                `socket.onopen = () => {`  
                    `badge.className = "px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20";`  
                    `badge.innerText = "CHANNELS OPENED";`  
                `};`

                `socket.onmessage = (event) => {`  
                    `const data = JSON.parse(event.data);`  
                    

                    `document.getElementById('stat-total').innerText = data.summary.total_sessions;`  
                    `document.getElementById('stat-approved').innerText = data.summary.approved_sessions;`  
                    `document.getElementById('stat-rejected').innerText = data.summary.rejected_sessions;`  
                    ``document.getElementById('stat-capital').innerText = `$` + data.summary.total_capital.toLocaleString();``

                    `cFunnel.data.datasets[0].data = [`  
                        `data.funnel_rejections.B_SENTINEL,`  
                        `data.funnel_rejections.E_SIGNAL,`  
                        `data.funnel_rejections.F_ATLAS,`  
                        `data.funnel_rejections.G_ANCHOR`  
                    `];`  
                    `cFunnel.update('none');`

                    `cLatency.data.datasets[0].data = Object.values(data.latencies);`  
                    `cLatency.update('none');`

                    `if (labelsBuffer.length >= 12) {`  
                        `labelsBuffer.shift(); streamSocial.shift(); streamFlow.shift();`  
                    `}`

                    `labelsBuffer.push(data.timestamp);`  
                    `streamSocial.push(data.divergence.social_velocity);`  
                    `streamFlow.push(data.divergence.onchain_flow);`  
                    `cDivergence.update('none');`

                    `if (data.active_positions.length === 0) {`  
                        `positionsBox.innerHTML = '<p class="text-slate-500 italic">No asset exposure logged currently...</p>';`  
                    `} else {`  
                        `` positionsBox.innerHTML = data.active_positions.map(p => ` ``  
                            `<div class="flex justify-between p-2 bg-slate-950 border border-slate-800 rounded">`  
                                `<div><span class="font-bold text-white">$${p.token_symbol}</span><p class="text-[9px] text-slate-500">Trigger Floor: ${p.entry_trigger}</p></div>`  
                                `<div class="text-right text-indigo-400 font-bold">$${p.allocated_usd}</div>`  
                            `</div>`  
                        `` `).join(''); ``  
                    `}`

                    `if (data.alerts.length === 0) {`  
                        `alertsBox.innerHTML = '<p class="text-slate-500 italic">No warnings active.</p>';`  
                    `} else {`

                        `` alertsBox.innerHTML = data.alerts.map(a => ` ``  
                            `<div class="p-1.5 rounded bg-amber-500/10 border border-amber-500/20 text-amber-300">`  
                                `<span class="font-bold text-rose-400">[${a.agent_name}]</span> ${a.message}`  
                            `</div>`  
                        `` `).join(''); ``  
                    `}`

                    `if (data.latest_log) {`  
                        `const line = document.createElement('div');`  
                        `line.className = "text-slate-300 leading-normal";`  
                        ``line.innerHTML = `<span class="text-slate-600">[${data.timestamp}]</span> ` + data.latest_log;``  
                        `consoleLog.appendChild(line);`  
                        `consoleLog.scrollTop = consoleLog.scrollHeight;`  
                    `}`  
                `};`

                `socket.onclose = () => {`  
                    `badge.className = "px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/20";`  
                    `badge.innerText = "DISCONNECTED";`

                `};`  
            `}`

            `window.onload = () => { initCharts(); initWS(); };`  
        `</script>`  
    `</body>`  
    `</html>`  
    `"""`  
    `return HTMLResponse(content=html_content)`  
