# App GUI de grabación y transcripción — decisiones

Mockup aceptado: [`docs/mockups/gui-mockup.html`](mockups/gui-mockup.html) (2026-10-30).

## Requisito base

Toda la grabación y la transcripción se hacen en local. La única excepción es el
resumen enviado a Notion (ver más abajo), que es una acción manual.

## Decisiones

| Área | Decisión | Motivo / notas |
| --- | --- | --- |
| UI | gpui (Rust). Ventana principal, popover, icono SNI (`ksni`) y widget para Noctalia. | La ventana y la barra son clientes de un daemon que graba; cerrar la ventana no corta la grabación. |
| Detección | Dos señales combinadas: (1) un navegador o Zoom abre un nodo de captura de PipeWire; (2) título de ventana por la IPC de niri/Hyprland. | Saber la plataforma (Meet/Teams/Zoom) no es importante; el título de ventana sirve sobre todo para nombrar la grabación. **Descartado en v1:** extensión del navegador. |
| Disparo | Sugerir siempre (notificación con acciones). Grabar sola solo con una regla explícita del usuario. | |
| Captura de pantalla | `xdg-desktop-portal` ScreenCast (`ashpd`) + PipeWire, recordando la ventana con `restore_token`. | Única vía correcta en Wayland. |
| Captura de audio | Dos pistas: micrófono (= «Yo») y monitor del sink (= «Otros»). | Separa «Yo/Otros» sin diarización. |
| Codificación | Elección automática: NVENC → VAAPI → software. El usuario puede cambiar el códec a mano si hay problemas de compatibilidad. | |
| ASR | `parakeet-tdt-0.6b-v3` (NVIDIA, 25 idiomas europeos con autodetección, incluido el español). | Sustituye a Whisper/WhisperX en esta app. Límite de ~4–5 min por pasada en TDT → trocear el audio (con VAD) y unir con marcas de tiempo. |
| Diarización | Parakeet no diariza. Se usa Sortformer (Nemotron 3 Diarization, hasta 8 voces) sobre la pista «Otros». | Runtime candidato: crate `parakeet-rs` (ONNX Runtime o burn), que ya trae TDT v3 + Sortformer. Alternativa: `sherpa-onnx`. |
| Voces guardadas | Biblioteca local de voces: embedding por hablante (TitaNet o similar vía `sherpa-onnx`), similitud coseno contra la biblioteca, umbral configurable (por defecto 0.75). Por debajo del umbral → «desconocida» con opción «guardar voz». | Reconocer a la misma persona entre reuniones. Las voces son datos biométricos: solo locales, con opción de borrar. |
| Transcripción en vivo | **No en v1.** Se transcribe al terminar la grabación (cola en segundo plano). | Parakeet TDT trabaja sobre la grabación completa; un borrador en vivo exigiría otro modelo de streaming. |
| Resumen | Acción manual «Resumen → Notion» que abre una sesión **interactiva** de Codex en una terminal, con la transcripción ya cargada. La base de Notion, los campos y el formato se deciden en ese chat con Codex; la app no guarda ninguna configuración de Notion. | **Rompe el «todo local»:** la transcripción se envía a OpenAI y a Notion. Por eso nunca es automático y pide confirmación. |
| Desarrollo | Lo implementa Claude entero, en `desktop/` (workspace de Cargo) dentro de este repo. | Decisión del usuario, 2026-10-30. |

## Entorno de destino

Hyprland, Noctalia 5 (plugins en Luau, `plugin_api = 3`), Brave, NVIDIA RTX 4070 SUPER (NVENC), PipeWire 1.6,
GStreamer 1.28, onnxruntime-cuda 1.29.

## Implementación (2026-10-31)

| Tema | Cómo quedó | Motivo |
| --- | --- | --- |
| Interfaz | `gpui-kit` 0.8 (gpui-component sobre `gpui-pre` 0.3.10). | gpui 0.2.2 de crates.io no trae campos de texto, desplegables ni diálogos. |
| Panel de grabación | Superficie *layer-shell* anclada arriba a la derecha (`actas popover`), sin el `Root` de gpui-component; si el compositor no tiene layer-shell, ventana flotante. | Se integra como los paneles de Noctalia y no necesita reglas de Hyprland. El `Root` añade un margen de sombra que descuadra la capa. |
| Hyprland | Reglas en Lua (`packaging/actas.lua`, `require("actas")`). | Hyprland 0.57 se configura en Lua. |
| Barra | Plugin de Noctalia en Luau que consulta `actas status --json` cada segundo; clic → panel, clic derecho → grabar/detener. Icono SNI además, para cualquier bandeja. | |
| Eco | Antes de transcribir se silencian los tramos de 30 ms del micrófono en los que el sistema suena ≥10 dB más fuerte; después se descartan frases del micrófono que repiten lo dicho por el sistema. | Sin cascos el micrófono capta a los demás, y Parakeet además pierde frases reales que siguen a un eco largo. |
| Troceado | VAD por energía, trozos ≤30 s, sin unir pausas >1,5 s. | Con trozos de 90 s Parakeet omitía frases enteras. |
| Alineación | `audiorate` antes de cada WAV. | PipeWire suspende salidas inactivas; sin rellenar, las pistas «Yo» y «los demás» se desalinearían. |
| `ashpd` | Feature `async-io` también en el demonio. | gpui ya lo usa con `async-io` y `ashpd` no admite `tokio` y `async-io` en la misma compilación. |
| Arranque de la interfaz | Un único proceso de interfaz residente (`actas --background`, lo lanza el demonio). `actas` y `actas popover` le pasan la orden por `$XDG_RUNTIME_DIR/actas-ui.sock` y salen. | Cada proceso gpui nuevo leía 1570 fuentes e iniciaba Vulkan: ~1 s en caliente, >3 s en frío. Con el proceso residente: panel en ~20 ms, ventana en ~5 ms. Además, varios clics seguidos ya no se anulan entre sí. |
| Tema | Plantilla de usuario de Noctalia (`packaging/noctalia-actas-theme.json` → `~/.config/actas/theme.json`), recargada en vivo; si no existe, colores de `~/.config/hypr/noctalia.lua`. Con transparencia «glass» en Noctalia, la ventana es translúcida. | La interfaz sigue el tema del escritorio sin configurarla. |
| Barra superior | «Actas» vuelve a la biblioteca. Grabar = botón principal + botón de solo audio; grabando = indicador con tiempo, pausa y detener. El estado del servicio, la cola y la promesa «sin red» pasan a una barra de estado inferior. | La píldora «sin red» parecía un botón y no hacía nada. |
| Portal | Hasta dos reintentos si el portal falla por algo que no sea una cancelación. | El backend de Hyprland arranca bajo demanda y la primera petición falló con «Invalid session». |
