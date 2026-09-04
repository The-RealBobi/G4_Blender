# Animaciones G4 — 2026-09-05

## Objetivo y restricciones
Reproducir y corregir la detección/aplicación de animaciones de y03150000
(Yo-Kai Watch 4), y determinar una aplicación fiel del root motion en Victory
Road contrastando Yo-Kai Gakuen Y. Trabajar exclusivamente en G4_Blender.
Conservar cambios previos de texturas; no modificar dumps ni ejecutables.
No considerar fijar la raíz a cero una reparación general. Distinguir pruebas
Blender de equivalencia con el juego. No publicar assets propietarios.

## Plan
1. **Completada: baseline y reproducción.** Guardar el estado previo en Git;
   inventariar los contenedores, clips, targets y canales del ejemplo y de
   muestras locales. Reproducir en Blender 5.2 la importación y T-pose.
2. **Completada para los fallos demostrados: diagnóstico binario.** Comparar offsets, versiones, hashes,
   jerarquías y canales entre juegos. Consultar Ghidra si la semántica no se
   deduce de datos; documentar hechos e hipótesis por separado.
3. **Completada: corrección localizada.** Corregir enumeración y enlace de
   acciones/esqueleto en sus propietarios actuales. Resolver transformaciones
   de raíz conservando bind pose, canales nativos y colocación del evento;
   no introducir heurísticas destructivas ni eliminar movimiento nativo.
4. **Completada en Blender: validación.** Añadir regresiones dirigidas de los fallos
   demostrados; ejecutar imports reales, muestrear poses y movimiento,
   generar renders comparables en varios frames de los tres juegos.
5. **Completada: entrega de la corrección verificada.** Actualizar versión según alcance, generar ZIP en
   dist, comprobar contenido e instalación/registro, registrar resultados,
   limitaciones y siguientes pasos aquí.

## Bitácora
- Repositorio independiente confirmado. Cambios previos en g4_port.py,
  g4_port_addon.py y tests/test_port_skinning_safety.py.
- La reparación anterior de root motion carecía de validación en el juego.
- Blender 5.2 disponible. MCP Ghidra no expuesto entre herramientas actuales;
  bridge local disponible para inspeccionar el transporte si se necesita.

- Baseline guardado: 40e329a (incluye cambios previos; no implica validación
  completa de dichos cambios).
- Fuente YK4 corregida por el usuario: /Volumes/BOBI/Proyectos Personales/YK4/._work/raw/
  y /Volumes/BOBI/Proyectos Personales/YK4/._work/cpk-stage/.
  y03150000 está en cpk-stage/0004/data/common/chr/y03150000; sus siete
  archivos coinciden byte a byte con el ZIP del reporte.
- p010 contiene 8 clips y p020 24, todos con 87 targets resueltos. El operador
  anterior solo importaba el índice elegido (0 por defecto).
- Blender 5.2 factory-startup con código baseline genera 870 curvas para
  戦1立ち1入 y cambia poses en frames 1/35/75. T-pose del reporte aún no
  reproducida; no confundir con reparación confirmada.
- Cambio en evaluación: importar todos los clips del banco y conservar la raíz
  en importaciones normales; omitir raíz solo si el flujo de eventos solicita
  explícitamente extraerla. Operador real p020 crea 24 acciones válidas.
- Renders de diagnóstico en /tmp/g4-animation-investigation/: la geometría
  mostrada es distinta de la captura del reporte incluso sin acción; hay que
  revisar la resolución de geometría antes de atribuirlo a animaciones.
- Ghidra activo mediante socket del bridge, programa nie.exe (61733 funciones).
  No se han modificado ejecutables ni programas Ghidra.

## Resultado de la intervención
- v1.5.0: opción de banco completo (activa por defecto), selección automática
  del primer clip independiente de más de dos frames cuando el campo está
  vacío, rango de cada Action y preservación de su asignación de huesos.
- Se mantiene el root en la importación normal. Solo el importador de eventos
  puede solicitar explícitamente su transferencia al objeto.
- La transferencia usa base_objeto * reposo_global * delta_local_corregido *
  inversa_reposo_global. No reaplica SOURCE_TO_BLENDER. Incluye la misma
  compensación de escala parental que la ruta de huesos. Si no se puede
  extraer por falta de reposo, se conserva la pista de hueso.
- Gakuen Y: c00010000_p010 contiene dos clips independientes y tres aditivos
  (sml/tll/pch). Estos últimos siguen sin base de mezcla y se omiten con aviso;
  no se interpretan como poses absolutas ni se aborta el resto del banco.

## Evidencias reproducibles
- tests/blender_animation_bank_smoke.py: recibe --model y --animation después
  del separador -- de Blender. Comprueba número de Actions contra el banco,
  selección inicial, curvas, rutas de huesos y slots cuando existen.
- Ejecutado en Blender 4.5.10 y 5.2: YK4 p010=8, p020=24; Gakuen p010=2
  independientes y 3 avisos por aditivos. Las seis ejecuciones pasan.
- tests/blender_animation_root_equivalence.py: mismos argumentos; comprueba
  raíz conservada en importación normal y equivalencia de matrices globales
  y vértices con extracción. Seis casos de ev72_50010: c11010019/c11010069,
  cortes c0010/c0040/c0050, primer frame, medio y final.
  Error máximo matrices: 9.54e-7; vértices: 1.52e-6 unidades Blender.
- Control negativo: la función de extracción de 40e329a falla el mismo caso
  c11010019/c0010 con error matricial 1.604991.
- Renders inspeccionados: YK4 frames 1/35/75; cabeza Victory Road 1/57/115;
  cabeza Gakuen (muestra de reposo). Son vistas de diagnóstico sin prueba de
  shading, escena completa ni correspondencia con cámara nativa.
- Sintaxis y git diff --check pasan. ZIP validado y descomprimido en carpeta
  aislada; su propio código importa p010 con ocho Actions en Blender 5.2.
- ZIP: dist/G4_Blender_v1.5.0.zip (36 archivos de producción, sin dumps,
  pruebas ni bitácora). SHA256:
  338989c98a34ca14dd669126065bbc94b53992ded6daf7d6b538f7fec9ea9033
- Logs y renders conservados en dist/animation_validation_20260905/.

## Límites y continuación
- No está demostrada la fidelidad dentro del juego. La equivalencia entre
  dos rutas Blender no prueba la semántica nativa completa de eventos.
- La T-pose concreta de la captura no se reproduce con el código de partida
  actual y el ZIP del reporte; el clip p020 ya tenía 870 curvas y movimiento.
  La captura muestra además otra apariencia que el modelo de ese ZIP. Para
  cerrar esa reproducción se necesita la escena/versión original del fallo.
- No se implementó mezcla aditiva, reconstrucción de puntos ausentes,
  colocación integral de eventos ni exportación de animaciones.
- Ghidra: se verificó nie.exe activo y las referencias de G4MT (141c28d60;
  1404bf410 es reinicialización). Esto no prueba aún el mezclador nativo;
  no usar esa consulta como evidencia de equivalencia del juego.
- Próxima fase de fidelidad: comparar un corte completo contra captura nativa
  y estudiar el mezclador aditivo con base conocida, conservando estos tests
  como controles. No sustituir automáticamente raíces por cero.
