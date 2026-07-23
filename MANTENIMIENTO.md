# Guía para futuras mejoras con Codex

Trabaja exclusivamente en `Bluxor-ai/radar-de-la-burbuja-ia`. No modifiques
`Bluxor-ai/ai-bubble-sentinel`.

Antes de publicar cambios:

1. conserva la separación entre fragilidad estructural, confirmación observable
   y mosaico de CapEx;
2. no presentes el índice como una probabilidad;
3. no sustituyas datos faltantes por cero;
4. conserva fecha, enlace, frescura y términos de cada fuente;
5. no publiques cotizaciones sin transformar, posiciones, cuentas, órdenes,
   correos, tokens, secretos o datos personales;
6. ejecuta `python -m pytest -q`;
7. prueba la construcción sin conexión y una actualización en vivo;
8. verifica `public/index.html`, `public/latest.json` y `public/history.csv`;
9. confirma que GitHub Actions y GitHub Pages terminen correctamente.
