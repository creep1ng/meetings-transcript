# Transcripción de reuniones a texto

Este repositorio procesa grabaciones de reuniones (por ejemplo de Microsoft Teams) para generar un archivo `.txt` con la transcripción completa.

Se utiliza Whisper para la transcripción y, cuando sea posible, se aprovechan aceleraciones por GPU (CUDA) y otras optimizaciones para equilibrar calidad y velocidad.

---

## Contenido

- Visión general
- Requisitos
- Instalación
- Uso
- Parámetros y opciones
- Recomendaciones para obtener mejores transcripciones
- Resolución de problemas
- Contribución y licencia

---

## Visión general

El script principal es [`main.py`](main.py:1). Su objetivo es tomar un archivo de audio o una exportación de grabación de reunión y producir un archivo `transcript.txt`.

---

## Requisitos

- Python 3.8+ (se recomienda Python 3.10+)
- Dependencias definidas en [`pyprojecct.toml`](pyprojecct.toml:1)
- GPU NVIDIA con CUDA (opcional, mejora velocidad) o CPU

---

## Instalación (rápida)

1. Clonar el repositorio y situarse en la carpeta del proyecto.
2. Crear y activar un entorno virtual:

```bash
python -m venv .venv
source .venv/bin/activate
```

3. Instalar dependencias (ejemplo genérico, adapte según su gestor):

```bash
pip install -r requirements.txt
```

Nota: si su proyecto usa un manejador como `pip-tools` o `poetry`, consulte [`pyprojecct.toml`](pyprojecct.toml:1) para detalles.

---

## Uso

El script principal es [`main.py`](main.py:1). El primer argumento positional es la ruta de entrada (un archivo de audio/video o una carpeta que contiene archivos compatibles). El comportamiento por defecto es crear/usar la carpeta `transcripts` (o la que se indique con `--output-dir`) y guardar por cada entrada un `.txt` con el mismo nombre base.

Ejemplos de ejecución:

```bash
# Transcribir un único archivo (se creará transcripts/recording.txt)
python main.py ruta/a/recording.wav

# Especificar modelo y forzar CPU
python main.py ruta/a/recording.mp4 --model medium --device cpu

# Procesar una carpeta completa (batch): cada archivo compatible dentro de la carpeta será transcrito
python main.py /ruta/a/carpeta_con_medias --model small --output-dir out

# Usar chunking para dividir audios largos en fragmentos de 120s y transcribir por partes
python main.py ruta/a/large_audio.wav --chunk 120
```

Notas importantes sobre ejecución y salida:

- Si se pasa una carpeta, el CLI recorrerá los archivos regulares dentro de ella y procesará solo los tipos soportados.
- Por defecto las transcripciones se guardan en la carpeta `transcripts` en la raíz del proyecto. Puede cambiarse con `--output-dir`.
- Para entradas de video (por ejemplo `.mp4`) se extraerá el audio y se guardará temporalmente en la carpeta de salida antes de transcribir.

---

## Parámetros y opciones (CLI)

Posicionales:

- `path` (obligatorio): Ruta a un archivo de audio/video o a una carpeta que contiene varios archivos a procesar.

Opciones principales:

- `--model`: Modelo Whisper a usar. Ejemplos: `tiny`, `base`, `small`, `medium`, `large`. Por defecto: `small`.
- `--device`: `cuda` o `cpu`. Si no se especifica, el script detecta automáticamente `cuda` si hay GPU disponible, si no usa `cpu`. Si se solicita `cuda` y no hay GPU, cae a `cpu` con advertencia.
- `--output-dir`: Directorio donde se guardarán los archivos `.txt` (por defecto `transcripts`).
- `--chunk`: (entero, segundos) Tamaño en segundos para dividir el audio en fragmentos y transcribir por partes. Valor por defecto `0` (sin chunking). Ejemplo: `--chunk 120` divide el audio en fragmentos de 120 segundos.
- `-h, --help`: Mostrar ayuda.

Comportamiento de modelos y recomendaciones:

- Modelos pequeños (`base`, `small`) consumen menos memoria y son más rápidos, pero la calidad de texto puede ser menor.
- Modelos medianos/grandes (`medium`, `large`) requieren más memoria/GPU pero producen mejores transcripciones en audio ruidoso o con varios hablantes.

Recomendación práctica:

- Si dispone de GPU con suficiente VRAM, use `--model medium` o superior y `--device cuda`.
- Para ejecuciones en CPU o entornos con pocos recursos, use `--model small` y `--device cpu`.

---

## Soporte de formatos y dependencias externas

Extensiones soportadas por el CLI:

- Audio: `.wav`, `.mp3`, `.m4a`, `.flac`, `.aac`, `.ogg`.
- Video: `.mp4`, `.mkv`, `.mov`, `.avi`, `.flv`, `.webm`.

Dependencias externas y herramientas adicionales:

- `ffmpeg` / `ffprobe`: Requeridas solo si se usa la opción `--chunk` (el script invoca `ffprobe` para obtener la duración y `ffmpeg` para recortar fragmentos). También `moviepy` usa `ffmpeg` para extraer audio de videos.
- `tqdm` es opcional: si está instalado el script mostrará una barra de progreso más informativa al transcribir por chunks o al transcurrir la tarea.

Si `ffmpeg`/`ffprobe` no están disponibles, el script intentará fallback (por ejemplo procesar sin chunking) pero la división por fragmentos puede no funcionar correctamente.

---

## Formato de salida

Por cada archivo procesado se crea un archivo `.txt` en el `--output-dir` con el mismo nombre base. El contenido actual es la transcripción simple (texto plano) resultante de Whisper y no incluye marcas de tiempo ni etiquetado de oradores en esta versión.

---

## Resolución de problemas comunes

- Error de memoria al cargar un modelo grande: bajar a un modelo (`medium`, `small`) o forzar `--device cpu`.
- Archivo no reconocido u omitido al pasar una carpeta: compruebe la extensión; el CLI solo procesa archivos con extensiones soportadas listadas arriba.
- Chunking no funciona o falla en la división: verifique que `ffprobe`/`ffmpeg` estén instalados y accesibles en PATH.

Si aparece un error relacionado con dependencias de Python, instale las requeridas con:

```bash
pip install -r requirements.txt
```

Si desea una experiencia de barra de progreso más rica, instale `tqdm`:

```bash
pip install tqdm
```

---

## Contribuir

Las contribuciones son bienvenidas. Para cambios:

1. Crear una rama nueva.
2. Añadir tests o indicaciones claras de los cambios.
3. Enviar un pull request.

Para centrarse en mejoras de reconocimiento o soporte de formatos, revise [`main.py`](main.py:1) y actualice la documentación en [`README.md`](README.md:1).

---

## Licencia

Indique aquí la licencia del proyecto (por ejemplo MIT). Si no hay una, añada un archivo `LICENSE` con la licencia deseada.

---

Archivo(s) clave:

- [`main.py`](main.py:1)
- [`pyprojecct.toml`](pyprojecct.toml:1)
- [`README.md`](README.md:1)