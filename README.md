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

El script principal es [`main.py`](main.py:1). Su objetivo es tomar un archivo de audio o una exportación de grabación de reunión y producir:

- Un archivo de texto con la transcripción (por ejemplo `transcript.txt`).

El flujo básico es:

1. Preparar la grabación/exportación (preferiblemente WAV, 16/44.1/48 kHz, PCM).
2. Ejecutar [`main.py`](main.py:1) con la ruta del audio.
3. Obtener el `.txt` con la transcripción.

---

## Requisitos

- Python 3.8+ (se recomienda Python 3.10+)
- Dependencias definidas en [`pyprojecct.toml`](pyprojecct.toml:1)
- GPU NVIDIA con CUDA (opcional, mejora velocidad) o CPU

Si desea verificar la versión de Python instalada:

- Usar la declaración para ejecutarlo localmente: [`python.declaration()`](main.py:1)

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

Ejemplo de ejecución básica (en la raíz del proyecto):

```bash
# Transcribir un archivo de audio y guardar en transcript.txt
python main.py --input ruta/a/grabacion.wav --output transcript.txt
```

Explicación rápida de las opciones más comunes:

- `--input`: ruta al archivo de audio (WAV, MP3 u otros soportados).
- `--output`: ruta del archivo `.txt` de salida.
- `--model`: (opcional) nombre del modelo Whisper a usar (por ejemplo `small`, `base`, `medium`, `large`).
- `--device`: (opcional) `cuda` para usar GPU o `cpu` para forzar CPU.

En caso de querer forzar el uso de CPU:

```bash
python main.py --input grabacion.wav --output transcript.txt --device cpu
```

---

## Parámetros y selección de modelo

- Modelos pequeños (`base`, `small`) consumen menos memoria y son más rápidos, pero la calidad de texto puede ser menor.
- Modelos medianos/grandes (`medium`, `large`) requieren más memoria/GPU pero producen mejores transcripciones en audio ruidoso o con varios hablantes.

Recomendación práctica:
- Si dispone de GPU con suficiente VRAM, use `--model medium` o superior.
- Para ejecuciones en CPU o entornos con pocos recursos, use `--model small`.

---

## Recomendaciones para mejores resultados

- Preferir grabaciones en PCM WAV a 16/44.1/48 kHz.
- Evitar compresión fuerte (evitar bitrates muy bajos en MP3).
- Minimizar ruido de fondo y ecos en la sala.
- Si la reunión tiene varios participantes con distintas voces, procurar que la grabación conserve separación de canales o buena inteligibilidad.

---

## Formato de salida

El archivo de salida es un `.txt` sencillo con la transcripción. El formato exacto (si incluye marcas de tiempo o etiquetas de orador) depende de la implementación en [`main.py`](main.py:1). Verifique el contenido del archivo generado después de la ejecución.

---

## Resolución de problemas comunes

- Error de memoria al cargar un modelo grande: bajar a un modelo `medium`/`small` o ejecutar en CPU.
- Archivo no reconocido: convertir la grabación a WAV PCM y volver a intentar.
- Velocidad lenta en CPU: reducir el tamaño del modelo o usar GPU (si está disponible).

Si aparece un error relacionado con dependencias, ejecutar:

```bash
pip install -r requirements.txt
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

Fin de la documentación de uso.

