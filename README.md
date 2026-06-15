# Bot Porra Mundial 2026 — ResultadosMundial_bot

Bot de Telegram que, durante la fase de grupos del Mundial 2026:

- **Al comienzo de cada partido** te manda tu pronóstico de la porra
  (el ganador que marcaste, en **negrita**; si pusiste empate, nadie en negrita).
- **Al terminar** te manda el resultado con ✅ si acertaste o ❌ si no.

Ejemplo:

```
Comienzo de 🇪🇸 España - Corea del Sur 🇰🇷
Final: 🇪🇸 España 1-0 Corea del Sur 🇰🇷 ✅
```

La porra está en [`data.py`](data.py) (extraída de `porra-marcos-gracia-arrondo.pdf`).
Los resultados y horarios se sacan de [football-data.org](https://www.football-data.org).

---

## Puesta en marcha (una sola vez)

### 1. Tu `chat_id`
Manda cualquier mensaje a **@ResultadosMundial_bot** en Telegram y luego:

```bash
python get_chat_id.py
```

Apunta el número que sale.

### 2. Token de football-data.org (gratis)
Regístrate en https://www.football-data.org/client/register (solo email).
Copia tu API token del correo / del panel.

### 3a. Opción recomendada — GitHub Actions (siempre encendido, gratis)
1. Crea un repo en GitHub y sube esta carpeta.
2. En el repo: **Settings → Secrets and variables → Actions → New repository secret**
   y crea estos tres secrets:
   - `TELEGRAM_TOKEN` → `8604008107:AAEi...` (el token del bot)
   - `TELEGRAM_CHAT_ID` → tu chat_id del paso 1
   - `FOOTBALL_DATA_TOKEN` → tu token del paso 2
3. Listo. El workflow [`.github/workflows/mundial.yml`](.github/workflows/mundial.yml)
   se ejecuta solo cada 5 minutos. La **primera** ejecución solo marca los partidos
   ya jugados como "ya avisados" (no manda nada del historial); a partir de ahí
   avisa de cada partido nuevo.

> Lánzalo a mano la primera vez desde la pestaña **Actions → Bot Porra Mundial →
> Run workflow** para que inicialice el estado.

### 3b. Opción alternativa — en tu PC
```bash
cp .env.example .env     # rellena TELEGRAM_CHAT_ID y FOOTBALL_DATA_TOKEN
pip install -r requirements.txt
```
Y prográmalo con el **Programador de tareas de Windows** para que ejecute
`python bot.py` cada 5 minutos (cargando las variables del `.env`).
Inconveniente: solo avisa mientras el PC esté encendido.

---

## Cómo funciona por dentro
- `bot.py` es **idempotente**: guarda en `state.json` qué avisos ya mandó, así que
  ejecutarlo de más nunca duplica mensajes.
- Solo actúa sobre partidos de **fase de grupos** que estén en tu porra.
- El "Comienzo" se dispara por la hora de inicio del partido; el "Final", cuando la
  API marca el partido como terminado (incluido el resultado real).
