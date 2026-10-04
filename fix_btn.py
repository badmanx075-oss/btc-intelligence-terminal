old_fn = """        async function runStrategySimulation() {""" 
new_fn = """        async function runStrategySimulation() { const btn = document.getElementById('sim-btn'); if(btn) btn.innerText = 'Simulating...'; try { const res = await fetch('/api/simulate_24h', { method: 'POST' }); await res.json(); } catch(e){} finally { if(btn) btn.innerText = '? Sim 24h Engine'; } }""" 
import re 
c = re.sub(r'async function runStrategySimulation\(\)\s*\{.*?finally\s*\{.*?\}\s*\}', new_fn.strip(), c, flags=re.DOTALL) 
with open('terminal_final.py', 'w', encoding='utf-8') as f: f.write(c) 
print('BUTTON_FIXED_SUCCESSFULLY') 
