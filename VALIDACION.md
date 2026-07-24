# Validación y robustez del Radar

## Qué sí se puede comprobar hoy

El Radar publica una lectura construida con siete bloques. La prueba de
robustez responde una pregunta concreta:

> ¿La conclusión cambia mucho dentro de los escenarios de pesos definidos?

El módulo `robustness.py` calcula esta prueba con la lectura viva. No guarda
resultados fijos ni presupone cuál será el nivel del Radar.

Incluye cuatro comprobaciones:

1. **Escenario A:** mueve cada peso entre 75% y 125% de su valor base. Después
   mantiene 35% para los cuatro bloques estructurales y 65% para los tres
   bloques de confirmación.
2. **Escenario B:** hace el mismo movimiento por bloque, pero deja que el peso
   estructural varíe entre 25% y 45%, con 35% como centro.
3. **Sin un bloque:** elimina cada bloque por turno y reparte su peso entre los
   demás del mismo grupo, conservando 35% de fragilidad y 65% de confirmación.
4. **Un peso a la vez:** reduce y aumenta cada peso 25%, repartiendo la
   diferencia de forma proporcional.

Las simulaciones usan por defecto 20,000 escenarios y la semilla `20260723`.
Con la misma lectura, número de escenarios y semilla, el resultado es idéntico.

## Cómo interpretar el resultado

Los percentiles 5, 50 y 95 muestran el intervalo donde cae la mayoría de las
lecturas bajo los pesos ensayados. También se informa qué porcentaje de esas
configuraciones conserva la banda original.

Ese porcentaje **no es una probabilidad de caída o ruptura**. Solo indica qué
tan estable es la etiqueta frente a los cambios de peso definidos. Los
escenarios tampoco demuestran que el Radar anticipe correctamente el futuro.

La prueba “sin un bloque” es deliberadamente más dura. Sirve para descubrir si
la conclusión depende demasiado de una sola pieza, incluso cuando los cambios
moderados de pesos parecen estables.

## Por qué todavía no existe un backtest completo honesto

Una reconstrucción histórica correcta debe usar únicamente la información que
ya estaba disponible en cada fecha. El Radar todavía combina datos con
frecuencias y calendarios distintos:

- precios y volumen diarios;
- NFCI semanal;
- CAPE, deuda de margen, concentración, oferta de acciones y otras lecturas
  lentas;
- fundamentales trimestrales;
- algunas señales manuales o incompletas.

Tomar el valor actual de una lectura lenta y colocarlo en años anteriores
introduciría información futura. También sería incorrecto tratar una
observación faltante como riesgo cero.

Por eso, una gráfica calculada hoy con datos revisados puede ser una
reconstrucción útil, pero no prueba capacidad predictiva fuera de muestra.

## Reconstrucción parcial del 1 al 22 de julio de 2026

No existen lecturas reales guardadas antes del 23 de julio. Lo que aparece del
1 al 22 de julio se calculó después y se muestra únicamente para dar contexto.
No debe describirse como “lo que marcaba el Radar ese día”.

La reconstrucción sigue estas reglas:

- los precios, volúmenes, VIX y curva de tasas se recortan en la fecha
  evaluada;
- esos archivos se descargaron después del periodo, por lo que pueden incluir
  correcciones o ajustes hechos posteriormente;
- el NFCI se incorpora con una demora de cinco días. Coincide con su calendario
  normal —miércoles a las 8:30 a. m. hora del Este para la semana terminada el
  viernes—, pero el archivo actual puede contener revisiones. La publicación
  pasa al jueves cuando corresponde por feriado;
- las cifras lentas se congelan con valores que se consideran disponibles al
  comenzar julio, pero no todas tienen una copia archivada con fecha y hora;
- la razón mercado/PIB usa dos datos del primer trimestre: el numerador estaba
  disponible el 11 de junio y el PIB usado quedó disponible el 25 de junio;
- la concentración usa 36.4%, cifra informada por S&P al 30 de junio. Se aplica
  a todo el tramo y no es una medición distinta para cada día;
- la oferta de acciones conserva 70 de 100 como decisión manual. La fuente
  oficial comprueba emisión y recompras, pero no esa conversión;
- el indicador de presión sobre gasto en inteligencia artificial no se
  reconstruye;
- fines de semana y días sin mercado repiten el último cierre disponible;
- cada fila lleva `observation_type=reconstructed` y un identificador separado;
- las lecturas automáticas originales no se reemplazan.

Además, el modelo, sus pesos y la lista de empresas fueron definidos después de
esas fechas. Por eso la serie usa información histórica, pero el diseño del
Radar sí conoce lo ocurrido después. Es una limitación distinta y no se elimina
con sólo recortar las cotizaciones.

Las fuentes, valores y límites se publican en
`data/historical_reconstruction.json`. La línea debe mostrarse punteada y con la
leyenda **Reconstrucción parcial**. Toda comparación que use uno de esos puntos
debe decir, por ejemplo, **contra una estimación reconstruida**. Las mediciones
formales de desempeño sólo deben usar lecturas reales guardadas desde el 23 de
julio.

La explicación oficial del [NFCI](https://www.chicagofed.org/-/media/publications/nfci/nfci-faqs-pdf.pdf)
confirma tanto el horario habitual como la posibilidad de revisiones. Para las
series de FRED, una comprobación estricta debe usar la versión histórica de
[ALFRED](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html), porque
la consulta normal de FRED devuelve lo que se conoce hoy sobre el pasado.

## Requisitos para llamar “verificada” a una fecha reconstruida

Una fecha sólo podrá subir de reconstrucción parcial a reconstrucción
verificada cuando cada dato utilizado conserve:

- el periodo que mide;
- la fecha y hora en que fue publicado;
- el valor tal como se publicó entonces, no sólo el valor corregido actual;
- una copia del archivo o una huella que permita comprobar que no cambió;
- la fuente y la fecha en que se descargó;
- la regla aplicada cuando faltó un dato.

El cálculo debe aceptar únicamente información publicada antes de la hora
evaluada. Un dato faltante se muestra como `N/D`, nunca como cero, y la cobertura
debe quedar visible. Si una fuente revisa el pasado, la reconstrucción publicada
no debe sobrescribirse en silencio: se crea una versión nueva y se conserva la
anterior.

## Registro prospectivo

La versión base del modelo comienza su seguimiento prospectivo el
**23 de julio de 2026**. Desde esa fecha, cada versión debe conservar:

- versión del modelo y huella de sus reglas;
- fecha programada, fecha de generación y fecha de mercado;
- resultado total y resultado de cada bloque;
- pesos y umbrales usados;
- insumos disponibles y su fecha de publicación;
- estado de calidad, faltantes y uso de respaldo;
- lecturas repetidas, aunque el resultado no cambie.

Una modificación de pesos, umbrales, universo o fórmula debe crear una nueva
versión. No debe reescribir el historial de una versión anterior.

Esta serie prospectiva será la evidencia más limpia porque las reglas quedan
congeladas antes de conocer los resultados posteriores.

## Sesgos que deben acompañar cualquier resultado histórico

- **Selección retrospectiva:** la cesta actual de empresas y fondos fue elegida
  con conocimiento del auge reciente de IA.
- **Supervivencia:** las empresas que hoy son importantes no representan
  necesariamente el universo que existía en cada fecha.
- **Revisiones:** algunas series macroeconómicas y fundamentales pueden cambiar
  después de su primera publicación.
- **Demoras de publicación:** el cierre de un trimestre no es la fecha en que
  el mercado conoció el dato.
- **Diseño posterior a episodios conocidos:** los pesos y umbrales actuales
  fueron definidos con crisis pasadas ya observadas.
- **Horizontes superpuestos:** muchas fechas semanales pueden corresponder al
  mismo episodio de caída y no son observaciones independientes.

Por estas razones, los resultados anteriores al registro prospectivo deben
etiquetarse como **exploratorios y retrospectivos**.

## Plan para validar el Radar contra QQQ y SMH

### 1. Crear un libro de datos punto en el tiempo

Cada lectura lenta necesita al menos:

`period_end, available_at, ingested_at, value, source, vintage_id, quality`

Para calcular una fecha histórica solo se admiten datos cuyo `available_at` sea
anterior o igual a esa fecha. Si falta el calentamiento necesario para un
retorno o media móvil, la señal se marca como no disponible.

### 2. Reconstruir una lectura semanal

Se calculará una observación por semana, conservando los siete bloques, su
cobertura y la versión exacta del modelo. Una fecha incompleta no participará en
las métricas del índice completo, pero seguirá registrada para mostrar la
cobertura real.

### 3. Medir resultados posteriores

Los resultados principales serán continuos:

- pérdida máxima desde la lectura durante 21, 63 y 126 sesiones;
- máximo drawdown dentro de esos horizontes;
- QQQ y SMH por separado.

Como lectura secundaria se podrán declarar umbrales operativos de estrés antes
de ejecutar el análisis. Los umbrales no convertirán el índice en una
probabilidad.

### 4. Publicar métricas comprensibles

- pérdida posterior mediana por banda;
- relación entre el nivel del Radar y el drawdown posterior;
- episodios precedidos por una alerta y anticipación observada;
- alertas no seguidas por estrés dentro del horizonte;
- tiempo total que el Radar permaneció en cada banda;
- intervalos de incertidumbre calculados por bloques de tiempo.

Los episodios solapados deben fusionarse para no contar una misma caída muchas
veces.

### 5. Comparar con referencias sencillas

La evaluación incluirá, con las mismas fechas:

- VIX por sí solo;
- QQQ respecto de su media de 200 sesiones;
- pesos iguales;
- subíndice de confirmación observable.

El objetivo es saber si el Radar aporta información adicional, no solo si se
mueve durante periodos que ya fueron volátiles.

## La presión sobre CapEx se valida por separado

El indicador de presión sobre CapEx no debe entrar en una conclusión retrospectiva
mientras no tenga al menos 70% de cobertura y datos con fecha real de
disponibilidad. Su resultado futuro debería compararse con cambios posteriores
en gasto reportado y guía corporativa, no con el precio de QQQ.

## Uso del módulo

```python
from robustness import analyze_weight_robustness

report = analyze_weight_robustness(live_result["blocks"])
```

El objeto devuelto contiene únicamente listas, diccionarios, cadenas, enteros,
flotantes y booleanos, por lo que puede publicarse directamente como JSON.
