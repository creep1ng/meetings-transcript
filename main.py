import os
import sys
from moviepy import VideoFileClip  # type: ignore
import whisper  # type: ignore
import torch

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


def transcribe_audio(audio_path, output_text_path, model_size="turbo"):
    """
    Transcribe un archivo de audio utilizando Whisper y guarda la transcripción en un archivo de texto.
    Usa CUDA si está disponible.
    """
    print("Preparando modelo Whisper...")

    # Determinar si hay una GPU disponible para usar CUDA
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Dispositivo detectado: {device}")

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
    # Verificar que se haya pasado un argumento desde la línea de comando
    if len(sys.argv) != 2:
        print("Uso: python main.py ruta/al/video")
        sys.exit(1)

    # Crear el directorio de salida si no existe
    ensure_output_directory()

    # Obtener la ruta completa al archivo de video desde los argumentos
    video_path = sys.argv[1]

    # Verificar si el archivo existe
    if not os.path.isfile(video_path):
        print(f"El archivo '{
              video_path}' no existe. Por favor verifica la ruta.")
        sys.exit(1)

    # Obtener el nombre base del archivo (sin extensión)
    base_name = os.path.splitext(os.path.basename(video_path))[0]

    # Crear las rutas de salida en la carpeta OUTPUT_DIR
    audio_output_path = os.path.join(OUTPUT_DIR, f"{base_name}.wav")
    transcription_output_path = os.path.join(OUTPUT_DIR, f"{base_name}.txt")

    # Proceso: 1) Extraer el audio, 2) Transcribir el audio
    extract_audio_from_video(video_path, audio_output_path)
    transcribe_audio(audio_output_path, transcription_output_path)

    print("¡Proceso completado exitosamente!")
    print(f"Resultados guardados en la carpeta: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
