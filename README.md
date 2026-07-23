# Radar de la Burbuja IA

Radar público, independiente y reproducible para observar fragilidad del auge
de inteligencia artificial. Separa dos preguntas:

- ¿El mercado tiene fragilidad estructural?
- ¿Ya existe confirmación observable de ruptura?

También muestra un mosaico separado sobre riesgo de moderación del CapEx de las
grandes tecnológicas. Los resultados son índices de 0 a 100: **no son
probabilidades, pronósticos exactos ni recomendaciones de inversión**.

## Sitio

**https://bluxor-ai.github.io/radar-de-la-burbuja-ia/**

La página se reconstruye a las 06:17 y 18:17, hora de Ciudad de México, mediante
GitHub Actions. Cada lectura expone fecha, calidad, cobertura, fuentes y
descargas en JSON/CSV.

## Qué calcula

El índice principal combina siete bloques:

| Bloque | Peso |
|---|---:|
| Valuación y expectativas | 12% |
| Concentración y subida temática | 10% |
| Apalancamiento y reversión | 8% |
| Oferta de nuevas acciones | 5% |
| Crédito y financiamiento | 25% |
| Ruptura interna del mercado | 25% |
| Volatilidad y ventas forzadas | 15% |

Los primeros cuatro forman la fragilidad estructural (35%) y los últimos tres
la confirmación observable (65%). La escala y todos los umbrales están
documentados en [METODOLOGIA.md](METODOLOGIA.md).

## Datos

Lecturas automáticas:

- Señales derivadas de SPY, QQQ, RSP, SMH, SOXX, ARKK, NVDA y megacaps mediante
  Yahoo Finance vía `yfinance`; no se redistribuyen series de cotizaciones.
- VIX y curva del Tesoro 10Y–2Y mediante FRED, con una lectura de mercado alterna
  para VIX y tasas oficiales del Tesoro como respaldo de la curva.
- National Financial Conditions Index (NFCI) de la Reserva Federal de Chicago.
- Medias móviles, amplitud, drawdowns, volatilidad, distribución y dos proxies
  del mosaico de CapEx.

Lecturas lentas y fechadas en `config.json`: CAPE, capitalización bursátil/PIB,
concentración top 10, margin debt, oferta de acciones y EBP. Si una lectura
vence, el sitio lo señala. Los componentes de CapEx sin una fuente pública,
fechada y redistribuible aparecen como `N/D` y se excluyen del cálculo; nunca se
convierten silenciosamente en cero.

## Ejecutar localmente

Requiere Python 3.12.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install --require-hashes -r requirements-lock.txt
python -m pytest -q
python radar.py --config config.json --output public --data-dir data
```

Para reconstruir el sitio con la última lectura guardada y sin pedir datos:

```bash
python radar.py --offline --config config.json --output public --data-dir data
```

## Privacidad y licencia

El proyecto publica indicadores agregados. No contiene posiciones, cuentas,
órdenes, correos, tokens, credenciales ni datos personales. Código bajo licencia
MIT; las fuentes externas conservan sus propios términos de uso.
