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
- **Oferta:** puntuación manual congelada a partir de datos de emisión y
  recompras. La fuente respalda los datos base, pero todavía no existe una
  fórmula pública que justifique su conversión a 70 de 100; por eso debe
  tomarse como una aproximación.
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

## Señales de presión sobre el CapEx

Es un índice separado. Combina gasto reciente de hyperscalers, pulso de
proveedores, construcción física, capacidad de nube, capacidad de financiar,
retorno contable y demanda financiada. Resume proxies actuales; no pronostica
por sí solo un recorte futuro.

Cinco señales se calculan automáticamente:

- crecimiento interanual del CapEx del trimestre más reciente de MSFT, GOOGL,
  AMZN y META, ponderado por gasto y con tope de 35% por empresa;
- rendimiento a 20 días de semiconductores frente a hyperscalers;
- promedio de tres meses de construcción privada de centros de datos de Census
  frente a los mismos tres meses del año anterior;
- mediana del flujo operativo dividido entre CapEx.
- mediana de un proxy contable amplio que combina ingreso operativo incremental
  y cambio en la rotación de PP&E.

La señal de construcción no observa cancelaciones ni conexiones eléctricas. El
proxy contable es de toda la empresa y no aísla exclusivamente el retorno de
IA. Esos límites se muestran en la interfaz.

El colector de Azure guarda precios públicos normales y Spot de una SKU H100,
regiones comparables, fechas efectivas y una huella de los precios. Se archivan
sin puntuar porque todavía no existe una metodología aprobada: un precio de
lista no demuestra inventario disponible. La demanda financiada/circularidad
sigue como `N/D` porque no existe una taxonomía pública uniforme y un buscador
de palabras produciría falsa precisión.

Una señal manual solo participa si tiene una lectura pública incorporada. Los
faltantes aparecen como `N/D`; las señales disponibles se renormalizan y el
porcentaje de cobertura queda visible. Una cobertura parcial debe interpretarse
con cautela. El tablero solo publica una conclusión principal cuando la
cobertura alcanza al menos 70%; antes muestra **datos insuficientes** y conserva
el cálculo parcial únicamente como referencia.

Las bandas de presión de CapEx son heurísticas y se validarán por separado:

| Intervalo | Etiqueta |
|---:|---|
| 0–34.99 | BAJO |
| 35–54.99 | VIGILAR |
| 55–69.99 | PREPARAR |
| 70–84.99 | ALERTA ALTA |
| 85–100 | CICLO DE RECORTE |

Estas etiquetas resumen los proxies medidos; no son probabilidades ni
pronósticos de guía futura.

## Frescura, fallos e historial

- Las fechas de mercado se toman de la observación común más antigua entre los
  instrumentos críticos.
- Las fechas macro se reportan de forma conservadora.
- Cada lectura lenta tiene una edad máxima en `config.json`.
- Si una fuente individual falla y existe una alternativa o lectura previa, se
  conserva con una advertencia.
- Si la actualización completa falla, el sitio usa el último estado guardado,
  lo marca como respaldo y no agrega una observación ficticia al historial.
- Cada ejecución válida se conserva aunque el índice se repita.
- Solo se deduplica el mismo `observation_id`, que identifica un reintento.
- Cada fila guarda la versión del modelo y los siete componentes. Las
  comparaciones usan únicamente la misma versión.
- Las referencias de 7 y 30 días usan una observación anterior o igual al
  momento objetivo y se omiten si queda a más de 18 horas.
- Las fechas del 1 al 22 de julio de 2026 son una reconstrucción parcial hecha
  después. En cada día se eliminan cierres posteriores, pero los archivos se
  descargaron más tarde y pueden contener correcciones. El NFCI usa una demora
  de cinco días, acorde con su publicación normal de los miércoles, y las
  cifras lentas provienen de un archivo público de supuestos. No todas cuentan
  con una copia guardada en su fecha original. Las series de FRED requieren la
  versión histórica de ALFRED para una comprobación estricta de lo que se sabía
  en cada fecha.
- El tablero muestra ese tramo con una línea punteada y la leyenda
  **Reconstrucción parcial**. Las lecturas guardadas automáticamente comienzan
  el 23 de julio y usan una línea sólida. Una comparación entre ambos tipos
  debe indicarlo de forma expresa.

## Robustez y validación

Cada lectura prueba 20,000 configuraciones de pesos con semilla fija. Una
variante conserva la división 35%/65%; otra permite que fragilidad varíe entre
25% y 45%. También se elimina cada bloque por turno y se cambia cada peso ±25%.
Los porcentajes resultantes describen configuraciones de peso, no probabilidades
de una caída.

La validación prospectiva de la versión 2.0.0 empieza el 23 de julio de 2026.
La reconstrucción de julio sirve para ver el recorrido reciente, pero no es una
prueba histórica del modelo ni demuestra que pueda anticipar una caída. El
modelo, sus pesos y la lista de empresas se definieron después de ese tramo.
Además, varios archivos actuales pueden contener revisiones de datos pasados.
Todavía no se publica una prueba histórica completa porque los datos lentos no
conservan todas sus fechas de publicación ni sus versiones originales. Aplicar
el valor corregido actual a una fecha anterior introduciría información que no
existía entonces. Véase [VALIDACION.md](VALIDACION.md).

## Fuentes y límites

El sitio enlaza cada fuente y publica únicamente valores configurados o
indicadores derivados. No redistribuye series de cotizaciones. Las fuentes
externas pueden revisar datos o cambiar sus interfaces; cada una conserva sus
propios términos.

El índice omite factores cualitativos, puede reaccionar tarde, y depende de
proxies. Debe usarse como tablero de vigilancia reproducible, no como sustituto
de investigación ni asesoría financiera.
