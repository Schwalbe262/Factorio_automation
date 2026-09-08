"""Local, read-only view of the actual deterministic supervisor checkpoint."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path


PAGE = r'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Factorio 자동화 진행</title><style>
:root{font-family:system-ui,sans-serif;color:#e8eef4;background:#121923;color-scheme:dark}body{max-width:1300px;margin:auto;padding:28px}h1{font-size:25px;margin:0 0 8px}p{color:#adb9c9}.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px}.card{background:#1a2533;border:1px solid #304156;border-radius:12px;padding:20px;margin-top:18px}canvas{width:100%;height:480px;background:#101a20;border-radius:8px}#status{display:inline-block;padding:5px 12px;background:#254750;border-radius:20px;color:#a3ebe0}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 5px;border-bottom:1px solid #304156}th{color:#9badc2;font-weight:500}#reason{color:#ffcf8b;overflow-wrap:anywhere}small{color:#9badc2}.metrics{display:flex;gap:35px;margin-top:20px}.metrics strong{font-size:27px;display:block}.metrics span{font-size:13px;color:#9badc2}button{background:#2c4157;border:1px solid #57718c;border-radius:7px;padding:9px 14px;cursor:pointer}.badge{display:inline-block;margin:4px 7px 4px 0;background:#2b3f50;border-radius:5px;padding:5px 8px;font-size:12px}@media(max-width:800px){.grid{grid-template-columns:1fr}body{padding:15px}canvas{height:340px}}
</style><h1>Factorio 자동화 진행</h1><p>실제 공장의 설비·생산·연구 상태를 2초마다 갱신합니다.</p>
<span id="status">연결 중</span><div class="metrics"><div><strong id="count">—</strong><span>관측 설비</span></div><div><strong id="minutes">—</strong><span>게임 시간(분)</span></div><div><strong id="techcount">—</strong><span>완료 연구</span></div></div>
<div class="grid"><section><div class="card"><h2>공장 배치</h2><canvas id="map"></canvas><small>주황: 제련 · 노랑: 채굴 · 파랑: 발전/급수 · 초록: 조립/연구 · 회색: 물류</small></div><div class="card"><h2>생산</h2><table><thead><tr><th>품목</th><th>누적 생산</th><th>누적 소비</th></tr></thead><tbody id="production"></tbody></table></div></section><aside><div class="card"><h2 id="stage">현재 단계</h2><p id="reason"></p><small id="address"></small><p><button onclick="refresh()">지금 새로고침</button></p></div><div class="card"><h2>실행기 재고</h2><table><tbody id="inventory"></tbody></table></div><div class="card"><h2>완료한 연구</h2><div id="techs"></div></div></aside></div><p id="updated"></p>
<script>
const $=id=>document.getElementById(id);const names={'iron-plate':'철판','copper-plate':'구리판','coal':'석탄','automation-science-pack':'빨간 과학팩','logistic-science-pack':'초록 과학팩','chemical-science-pack':'파란 과학팩','electronic-circuit':'전자 회로','rocket-part':'로켓 부품'};
function table(id,rows){$(id).replaceChildren(...rows.map(row=>{let tr=document.createElement('tr');row.forEach(v=>{let td=document.createElement('td');td.textContent=v;tr.append(td)});return tr}))}
function draw(entities){let c=$('map'),r=c.getBoundingClientRect();c.width=r.width*devicePixelRatio;c.height=r.height*devicePixelRatio;let ctx=c.getContext('2d');ctx.scale(devicePixelRatio,devicePixelRatio);let es=entities.filter(e=>e.position);if(!es.length)return;let xs=es.map(e=>e.position.x),ys=es.map(e=>e.position.y),x0=Math.min(...xs)-6,y0=Math.min(...ys)-6,scale=Math.min(r.width/(Math.max(...xs)-x0+6),r.height/(Math.max(...ys)-y0+6));ctx.fillStyle='#172a29';ctx.fillRect(0,0,r.width,r.height);for(let e of es){let n=e.name,x=(e.position.x-x0)*scale,y=(e.position.y-y0)*scale,size=n.includes('furnace')||n.includes('drill')?2:n.includes('engine')||n==='boiler'||n.includes('assembling')||n==='lab'?3:1;ctx.fillStyle=n.includes('furnace')?'#dc9455':n.includes('drill')?'#edca6f':n.includes('engine')||n==='boiler'||n.includes('pump')?'#66b6de':n.includes('assembling')||n==='lab'?'#81c998':'#8393a3';ctx.fillRect(x-size*scale/2,y-size*scale/2,Math.max(2,size*scale),Math.max(2,size*scale));if(e.status_name==='no_fuel'){ctx.fillStyle='#fd6464';ctx.beginPath();ctx.arc(x,y,Math.max(2,scale*.35),0,7);ctx.fill()}}}
async function refresh(){try{let response=await fetch('/status',{cache:'no-store'});if(!response.ok)throw new Error(response.status);let s=await response.json();$('status').textContent=({running:'진행 중',waiting:'대기 중',blocked:'문제 해결 필요',succeeded:'지정 단계 완료',failed:'실패',not_started:'실행 대기'})[s.status]||s.status;$('stage').textContent=({bootstrap:'초기 채굴·기술 해금',power:'연속 발전',production:'생산·연구 확장',launch:'로켓 발사'})[s.stage]||'현재 단계';$('reason').textContent=s.reason||'';$('count').textContent=(s.entities||[]).length;$('minutes').textContent=((s.tick||0)/3600).toFixed(1);$('techcount').textContent=(s.technologies||[]).length;$('address').textContent=s.server_address?'게임 접속: '+s.server_address:'';table('inventory',Object.entries(s.inventory||{}).map(([n,c])=>[names[n]||n,c]));table('production',Object.entries(s.production||{}).map(([n,c])=>[names[n]||n,c.produced,c.consumed]));$('techs').replaceChildren(...(s.technologies||[]).map(t=>{let el=document.createElement('span');el.className='badge';el.textContent=t;return el}));draw(s.entities||[]);$('updated').textContent='마지막 조회 '+new Date().toLocaleTimeString()+' · 변경된 코드의 재실행/검증 중에는 상태가 잠시 멈출 수 있습니다.'}catch(e){$('status').textContent='상태를 읽을 수 없습니다';$('reason').textContent=String(e)}}refresh();setInterval(refresh,2000);window.addEventListener('resize',refresh);
</script></html>'''


def serve(runtime: Path, host: str = "127.0.0.1", port: int = 18890) -> None:
    status_path = Path(runtime) / "status.json"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in {"/", "/index.html"}:
                payload = PAGE.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            elif self.path == "/status":
                try:
                    raw = json.loads(status_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    raw = {"status": "not_started", "reason": "아직 실행 기록이 없습니다."}
                except (OSError, ValueError):
                    self.send_error(503, "Status temporarily unavailable")
                    return
                payload = json.dumps(raw, ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18890)
    args = parser.parse_args()
    serve(args.runtime, port=args.port)
