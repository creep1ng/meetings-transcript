import argparse
import os
import sys

import torch
import whisper  # type: ignore
from moviepy import VideoFileClip  # type: ignore

# Variable global para la carpeta de salida
OUTPUT_DIR = "transcripts"


def ensure_output_directory():
    """
    Crea la carpeta de salida si no existe.
    """
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"Directorio '{OUTPUT_DIR}' creado para guardar los resultados.")


def extract_audio_from_video(video_path, output_audio_path):
    """
    Extrae el audio de un archivo de video y lo guarda en un archivo de audio.
    """
    try:
        video = VideoFileClip(video_path)
        audio = video.audio
        audio.write_audiofile(output_audio_path)
        audio.close()
        video.close()
        print(f"Audio extraído y guardado en: {output_audio_path}")
    except Exception as e:
        print(f"Error al extraer audio: {e}")
        sys.exit(1)


def transcribe_audio(
    audio_path, output_text_path, model_size="turbo", device: str = "cpu"
):
    """
    Transcribe un archivo de audio utilizando Whisper y guarda la transcripción en un archivo de texto.
    Usa CUDA si está disponible.
    """
    print("Preparando modelo Whisper...")

    # Device se recibe desde la llamada (puede venir de .env o de argumentos)
    # Si se solicita 'cuda' pero no hay GPU disponible, caer a 'cpu'.
    if device == "cuda" and not torch.cuda.is_available():
        print(
            "Advertencia: se solicitó 'cuda' pero no hay GPU disponible. Usando 'cpu' en su lugar."
        )
        device = "cpu"
    print(f"Dispositivo seleccionado: {device}")

    try:
        # Cargar el modelo de Whisper en el dispositivo seleccionado (GPU/CPU)
        model = whisper.load_model(model_size, device=device)

        print(f"Transcribiendo audio con el modelo '{model_size}'...")
        result = model.transcribe(audio_path, language="es")
        transcript = result["text"]

        # Guardar la transcripción en un archivo de texto
        with open(output_text_path, "w", encoding="utf-8") as f:
            f.write(transcript)

        print(f"Transcripción guardada en: {output_text_path}")
    except Exception as e:
        print(f"Error al transcribir audio: {e}")
        sys.exit(1)


def main():
    # Cargar variables de entorno desde un posible archivo .env en la raíz
    def load_dotenv(path=".env"):
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Transcribir grabaciones de reuniones a texto"
    )
    parser.add_argument(
        "video", help="Ruta al archivo de video (o audio) a transcribir"
    )
    parser.add_argument(
        "--model", help="Modelo Whisper a usar (ej: small, medium, large)"
    )
    parser.add_argument(
        "--device", choices=["cuda", "cpu"], help="Dispositivo para ejecutar (cuda|cpu)"
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help="Directorio de salida para transcripciones",
    )
    args = parser.parse_args()

    # Crear el directorio de salida si no existe
    OUTPUT = args.output_dir
    global OUTPUT_DIR
    OUTPUT_DIR = OUTPUT
    ensure_output_directory()

    # Obtener la ruta completa al archivo de video desde los argumentos
    video_path = args.video

    # Verificar si el archivo existe
    if not os.path.isfile(video_path):
        print(f"El archivo '{video_path}' no existe. Por favor verifica la ruta.")
        sys.exit(1)

    # Obtener el nombre base del archivo (sin extensión)
    base_name = os.path.splitext(os.path.basename(video_path))[0]

    # Crear las rutas de salida en la carpeta OUTPUT_DIR
    audio_output_path = os.path.join(OUTPUT_DIR, f"{base_name}.wav")
    transcription_output_path = os.path.join(OUTPUT_DIR, f"{base_name}.txt")

    # Proceso: 1) Extraer el audio, 2) Transcribir el audio
    extract_audio_from_video(video_path, audio_output_path)

    # Determinar opciones de modelo y dispositivo: prioridad -> args -> .env -> por defecto
    model = args.model or os.environ.get("MODEL", "small")
    env_device = args.device or os.environ.get("DEVICE")
    if env_device:
        device = env_device
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    transcribe_audio(
        audio_output_path, transcription_output_path, model_size=model, device=device
    )

    print("¡Proceso completado exitosamente!")
    print(f"Resultados guardados en la carpeta: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
