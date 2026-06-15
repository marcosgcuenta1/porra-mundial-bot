# Bot Porra Mundial 2026 — ResultadosMundial_bot

Bot de Telegram **multi-usuario**: cualquier participante de la porra puede usarlo.
Durante la fase de grupos del Mundial 2026, a cada uno le manda:

- **Al comienzo de cada partido**, SU pronóstico (equipos en código FIFA de 3 letras,
  su ganador en **negrita**; empate: nadie en negrita).
- **Al terminar**, el resultado (✅ si acertó, ❌ si no) y SU zona de la clasificación.

**Cómo se apunta un participante:** abre **@ResultadosMundial_bot**, pulsa *Start*,
escribe su nombre, y confirma con el botón. A partir de ahí recibe los avisos de los
siguientes partidos (no el historial). Para cambiar de identidad: `/start`.

Ejemplo:

```
Comienzo de 🇪🇸 ESP - KOR 🇰🇷

✅ Final: 🇪🇸 ESP 1-0 KOR 🇰🇷

Clasificación actual
4º Fulano De Tal — 9 pts
5º Mengano Perez — 6 pts
6º Marcos Gracia Arrondo — 6 pts   (en negrita)
7º Zutano Lopez — 3 pts
8º Perengano Ruiz — 3 pts
```

Todos los datos salen de la web oficial
[NFQ World Cup](https://cesaresteban.github.io/NFQ-WORLD-CUP/): los participantes y sus
porras desde su Supabase, y los resultados/horarios desde su misma API de partidos
(`sports.bzzoiro.com`). Así la clasificación coincide exactamente con la de la web
(3 puntos por cada 1/X/2 acertado en grupos). No hace falta registrarse en ningún sitio.

> Solo necesita el secret `TELEGRAM_TOKEN` para funcionar.

---

## Puesta en marcha (ya hecha)

Alojado en **GitHub Actions** (cron cada 5 min), siempre encendido y gratis. Único
secret necesario: `TELEGRAM_TOKEN` (en *Settings → Secrets and variables → Actions*).
La **primera** ejecución solo marca los partidos ya jugados como "ya avisados" (no manda
nada del historial); a partir de ahí avisa de cada partido nuevo a todos los identificados.

Para ejecutarlo en local: `pip install -r requirements.txt` y `TELEGRAM_TOKEN=... python bot.py`.

---

## Cómo funciona por dentro
- `bot.py` es **idempotente**: guarda en `state.json` los usuarios y qué avisos ya mandó,
  así que ejecutarlo de más nunca duplica mensajes.
- Solo actúa sobre partidos de **fase de grupos**.
- El "Comienzo" se dispara por la hora de inicio del partido; el "Final", cuando la
  API marca el partido como terminado (con el resultado real).
