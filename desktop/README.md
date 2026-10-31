# Actas

Graba y transcribe tus reuniones (Meet, Teams, Zoom…) **en local**: captura de pantalla y audio en Wayland,
transcripción con Parakeet TDT 0.6B v3, separación de hablantes con Sortformer y reconocimiento de voces entre
reuniones. Decisiones de diseño: [`../docs/gui-decisions.md`](../docs/gui-decisions.md). Mockup:
[`../docs/mockups/gui-mockup.html`](../docs/mockups/gui-mockup.html).

## Piezas

| Pieza | Qué hace |
| --- | --- |
| `actasd` | Demonio: detecta llamadas, graba, transcribe en cola, icono en la bandeja (SNI) y notificaciones. |
| `actas` | Ventana (gpui), panel de grabación (`actas popover`) y línea de órdenes. |
| `noctalia-plugin/` | Widget de barra para Noctalia 5. |
| `crates/actas-core` | Configuración, base de datos SQLite, protocolo IPC, exportaciones. |
| `crates/actas-engine` | Transcripción: VAD, Parakeet, Sortformer, huellas de voz CAM++. |

```
 Noctalia / bandeja ──┐
 actas (GUI, CLI) ────┼── socket $XDG_RUNTIME_DIR/actas.sock ──► actasd ──► GStreamer (portal + PulseAudio)
                      │                                             └──► cola ──► actas-engine (ONNX Runtime, CUDA)
                      └── ~/.local/share/actas/actas.db (SQLite, WAL) ◄──┘
```

## Instalación

Requisitos (Arch): `rust`, `gstreamer`, `gst-plugins-{base,good,bad,ugly}`, `gst-plugin-pipewire`,
`onnxruntime-cuda` (o `onnxruntime` para CPU), `ffmpeg`, `xdg-desktop-portal-hyprland`.

```sh
./install.sh                 # compila, instala en ~/.local/bin y arranca el servicio
actas models download        # ≈3 GB; es la única operación que usa la red
```

Después, en Noctalia: Ajustes → Barra → añadir el widget «Actas». En Hyprland, añade
`require("actas")` a `~/.config/hypr/hyprland.lua` (reglas del panel de grabación).

## Uso

- **Detección**: cuando un navegador (o Zoom, Teams, Discord…) abre el micrófono durante 5 s, Actas te sugiere
  grabar. El título de la ventana (p. ej. «Meet – Weekly sync») da nombre a la grabación. «Siempre con este título»
  crea una regla para grabar sin preguntar.
- **Barra**: clic → panel de grabación; clic derecho → grabar/detener. En la bandeja: clic central → grabar/detener.
- **Pistas**: tu micrófono («Yo») y el audio del sistema («los demás») se graban por separado. El eco que tu
  micrófono capta de los altavoces se silencia antes de transcribir.
- **Voces**: en una transcripción, «Guardar voz» asocia la huella de un hablante a un nombre; en las siguientes
  reuniones se reconoce sola (umbral en Ajustes).
- **Resumen → Notion**: abre una sesión interactiva de Codex en una terminal con la transcripción cargada; el
  destino en Notion se decide en esa conversación. **Es lo único que envía la transcripción fuera del equipo.**

Línea de órdenes: `actas help`.

## Ficheros

| Ruta | Contenido |
| --- | --- |
| `~/.config/actas/config.toml` | Configuración (la escribe la ventana de Ajustes). |
| `~/.local/share/actas/actas.db` | Biblioteca: reuniones, transcripciones, voces. |
| `~/.local/share/actas/recordings/<id>/` | `recording.mkv` (vídeo + 2 pistas Opus), `mic.wav`, `system.wav`, `transcript.md`. |
| `~/.local/share/actas/models/` | Modelos ONNX. |
| `~/.local/state/actas/screencast-restore-token` | Ventana elegida en el portal (bórralo o usa «Elegir otra ventana»). |

## Desarrollo

```sh
cargo test --workspace
cargo run -p actas-engine --release --example transcribe -- mic.wav system.wav   # prueba del motor
RUST_LOG=actasd=debug cargo run -p actasd
```

`ORT_DYLIB_PATH` apunta a otra `libonnxruntime.so` si no está en `/usr/lib`.
