# type: ignore
from moviepy import VideoFileClip
from torch import device
import whisper
import os
import sys


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


def transcribe_audio(audio_path, output_text_path, model_size="base"):
    """
    Transcribe un archivo de audio utilizando Whisper y guarda la transcripción en un archivo de texto.
    """
    print(f"Transcribiendo audio con Whisper usando el modelo '{
          model_size}'...")
    try:
        model = whisper.load_model(model_size)
        # Asegúrate de especificar el idioma (en este caso, español)
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

    # Obtener la ruta completa al archivo de video desde los argumentos
    video_path = sys.argv[1]

    # Verificar si el archivo existe
    if not os.path.isfile(video_path):
        print(f"El archivo '{
              video_path}' no existe. Por favor verifica la ruta.")
        sys.exit(1)

    # Obtener el nombre base del archivo (sin extensión) y su directorio
    base_name = os.path.splitext(os.path.basename(video_path))[0]

    # Crear los nombres de salida para el archivo de audio y la transcripción
    audio_output_path = f"{base_name}.wav"
    transcription_output_path = f"{base_name}.txt"

    # Proceso: 1) Extraer el audio, 2) Transcribir el audio
    extract_audio_from_video(video_path, audio_output_path)
    transcribe_audio(audio_output_path, transcription_output_path)

    print("¡Proceso completado exitosamente!")


if __name__ == "__main__":
    main()
