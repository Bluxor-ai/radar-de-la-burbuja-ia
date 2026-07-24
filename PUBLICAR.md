# Publicación y operación

El repositorio canónico es:

**https://github.com/Bluxor-ai/radar-de-la-burbuja-ia**

El sitio se publica en:

**https://bluxor-ai.github.io/radar-de-la-burbuja-ia/**

## Flujo automático

El workflow `Actualizar y publicar el Radar`:

1. instala dependencias desde el archivo bloqueado con hashes;
2. ejecuta las pruebas;
3. obtiene los datos y construye `public/`;
4. guarda el estado derivado en `data/` y `public/`, incluidos detalles
   contables por empresa y precios públicos de GPU por región necesarios para
   reproducir las señales; no guarda cotizaciones crudas ni datos personales;
5. despliega el artefacto en GitHub Pages.

Corre a las 06:17 y 18:17, hora de Ciudad de México. También puede ejecutarse
desde **Actions → Actualizar y publicar el Radar → Run workflow**.

## Si una actualización falla

1. Abre la ejecución fallida en **Actions**.
2. Revisa cuál proveedor figura como no disponible.
3. Vuelve a ejecutar el workflow si parece una interrupción temporal.
4. Actualiza las lecturas lentas de `config.json` si el sitio las marca como
   vencidas, conservando fecha y enlace público.

Un fallo completo no agrega puntos artificiales al historial. La última lectura
guardada permanece disponible y se identifica como respaldo. El despliegue se
completa para no dejar el sitio caído, pero el job final `health` marca la
ejecución en rojo para que el incidente no pase inadvertido.

## Regla de separación

Este proyecto es independiente. No copies su historial, workflow ni datos a
`ai-bubble-sentinel`. No agregues posiciones, cuentas, órdenes, correos,
credenciales o archivos `.env`.
