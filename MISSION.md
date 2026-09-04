# Animaciones G4 — 2026-09-05

## Objetivo y restricciones
Reproducir y corregir la detección/aplicación de animaciones de y03150000
(Yo-Kai Watch 4), y determinar una aplicación fiel del root motion en Victory
Road contrastando Yo-Kai Gakuen Y. Trabajar exclusivamente en G4_Blender.
Conservar cambios previos de texturas; no modificar dumps ni ejecutables.
No considerar fijar la raíz a cero una reparación general. Distinguir pruebas
Blender de equivalencia con el juego. No publicar assets propietarios.

## Plan
1. **Activa: baseline y reproducción.** Guardar el estado previo en Git;
   inventariar los contenedores, clips, targets y canales del ejemplo y de
   muestras locales. Reproducir en Blender 5.2 la importación y T-pose.
2. **Pendiente: diagnóstico binario.** Comparar offsets, versiones, hashes,
   jerarquías y canales entre juegos. Consultar Ghidra si la semántica no se
   deduce de datos; documentar hechos e hipótesis por separado.
3. **Pendiente: corrección localizada.** Corregir enumeración y enlace de
   acciones/esqueleto en sus propietarios actuales. Resolver transformaciones
   de raíz conservando bind pose, canales nativos y colocación del evento;
   no introducir heurísticas destructivas ni eliminar movimiento nativo.
4. **Pendiente: validación.** Añadir regresiones dirigidas de los fallos
   demostrados; ejecutar imports reales, muestrear poses y movimiento,
   generar renders comparables en varios frames de los tres juegos.
5. **Pendiente: entrega.** Actualizar versión según alcance, generar ZIP en
   dist, comprobar contenido e instalación/registro, registrar resultados,
   limitaciones y siguientes pasos aquí.

## Bitácora
- Repositorio independiente confirmado. Cambios previos en g4_port.py,
  g4_port_addon.py y tests/test_port_skinning_safety.py.
- La reparación anterior de root motion carecía de validación en el juego.
- Blender 5.2 disponible. MCP Ghidra no expuesto entre herramientas actuales;
  bridge local disponible para inspeccionar el transporte si se necesita.
