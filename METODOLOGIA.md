# Metodología

## Propósito

El Radar resume señales heterogéneas en escalas comparables de 0 a 100. Un valor
alto significa mayor fragilidad o mayor confirmación de ruptura según el bloque;
no significa una probabilidad exacta de caída.

Cada señal continua se transforma así:

`índice = limitar((valor − umbral_bajo) / (umbral_alto − umbral_bajo) × 100, 0, 100)`

Los umbrales son reglas públicas y deterministas en `radar.py`. No hay un modelo
entrenado con información privada.

## Índice principal

La fórmula es:

`12% valuación + 10% concentración + 8% apalancamiento + 5% oferta + 25% condiciones financieras + 25% ruptura interna + 15% presión vendedora`

- **Valuación:** promedio de CAPE y capitalización bursátil/PIB.
- **Concentración:** 60% participación del top 10 y 40% rendimiento relativo
  máximo, a dos años, de QQQ/SMH/SOXX/NVDA contra SPY.
- **Apalancamiento:** relación deuda/crédito libre, crecimiento anual de margin
  debt y una lectura explícita de rollover.
- **Oferta:** emisión bruta ajustada por absorción de recompras.
- **Condiciones financieras:** 50% nivel del NFCI, 25% EBP fechado y 25%
  endurecimiento del NFCI en cuatro semanas.
- **Ruptura interna:** amplitud bajo medias de 50/200 días y situación de QQQ y
  SMH respecto de ambas medias.
- **Presión vendedora:** días de distribución, régimen de
  volatilidad/drawdown y nivel del VIX. Es una aproximación de mercado; no
  prueba que todas las ventas sean forzadas.

La **fragilidad estructural** renormaliza los primeros cuatro bloques, cuyo peso
base suma 35%. La **confirmación observable** renormaliza los últimos tres, cuyo
peso base suma 65%.

## Regímenes

| Intervalo | Etiqueta |
|---:|---|
| 0–34.99 | NORMAL |
| 35–49.99 | VIGILAR |
| 50–64.99 | PREPARAR |
| 65–79.99 | ALERTA ALTA |
| 80–100 | ALERTA CRÍTICA |

Las etiquetas describen bandas del índice, no certeza de un evento futuro.

## Riesgo de recorte de CapEx

Es un índice separado. Combina guía de hyperscalers, pulso de proveedores,
construcción física, capacidad de nube, capacidad de financiar, retorno
económico y demanda financiada.

Dos señales se calculan automáticamente:

- rendimiento a 20 días de semiconductores frente a hyperscalers;
- mediana del flujo operativo dividido entre CapEx.

Una señal manual solo participa si tiene una lectura pública incorporada. Los
faltantes aparecen como `N/D`; las señales disponibles se renormalizan y el
porcentaje de cobertura queda visible. Una cobertura parcial debe interpretarse
con cautela. El tablero solo publica una conclusión principal cuando la
cobertura alcanza al menos 70%; antes muestra **datos insuficientes** y conserva
el cálculo parcial únicamente como referencia.

## Frescura, fallos e historial

- Las fechas de mercado se toman de la observación común más antigua entre los
  instrumentos críticos.
- Las fechas macro se reportan de forma conservadora.
- Cada lectura lenta tiene una edad máxima en `config.json`.
- Si una fuente individual falla y existe una alternativa o lectura previa, se
  conserva con una advertencia.
- Si la actualización completa falla, el sitio usa el último estado guardado,
  lo marca como respaldo y no agrega una observación ficticia al historial.
- Lecturas idénticas se deduplican para evitar que reintentos inflen la serie.

## Fuentes y límites

El sitio enlaza cada fuente y publica únicamente valores configurados o
indicadores derivados. No redistribuye series de cotizaciones. Las fuentes
externas pueden revisar datos o cambiar sus interfaces; cada una conserva sus
propios términos.

El índice omite factores cualitativos, puede reaccionar tarde, y depende de
proxies. Debe usarse como tablero de vigilancia reproducible, no como sustituto
de investigación ni asesoría financiera.
