import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Tuple

import torch
import whisper  # type: ignore
from moviepy import VideoFileClip  # type: ignore

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - optional dependency
    tqdm = None

# Variable global para la carpeta de salida
OUTPUT_DIR = "transcripts"

# Extensiones soportadas
SUPPORTED_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg")
SUPPORTED_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm")


def ensure_output_directory():
    """
    Crea la carpeta de salida si no existe.
    """
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"Directorio '{OUTPUT_DIR}' creado para guardar los resultados.")


def is_audio_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_AUDIO_EXTS


def is_video_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_VIDEO_EXTS


def extract_audio_from_video(video_path: str, output_audio_path: str) -> None:
    """
    Extrae el audio de un archivo de video y lo guarda en un archivo de audio.
    Lanza excepciones en caso de error para que el llamador pueda decidir cómo manejarlo.
    """
    try:
        video = VideoFileClip(video_path)
        audio = video.audio
        audio.write_audiofile(output_audio_path)
        audio.close()
        video.close()
        print(f"Audio extraído y guardado en: {output_audio_path}")
    except Exception as e:
        raise RuntimeError(f"Error al extraer audio de '{video_path}': {e}")


def transcribe_audio(
    audio_path: str,
    output_text_path: str,
    model_size: str = "turbo",
    device: str = "cpu",
    chunk_length_seconds: int = 0,
) -> None:
    """
    Transcribe un archivo de audio utilizando Whisper y guarda la transcripción en un archivo de texto.
    Usa CUDA si está disponible.
    Lanza excepciones en caso de error para que el llamador pueda manejar fallos por archivo.
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

    # Cargar el modelo de Whisper en el dispositivo seleccionado (GPU/CPU)
    model = whisper.load_model(model_size, device=device)

    print(
        f"Iniciando transcripción con el modelo '{model_size}' (archivo: {audio_path})"
    )

    # Si se solicita chunking, dividimos el audio en fragmentos y transcribimos
    # fragmento por fragmento actualizando una barra tqdm con progreso real.
    if chunk_length_seconds and chunk_length_seconds > 0:
        # Crear carpeta temporal para chunks
        tmp_dir = os.path.join(
            OUTPUT_DIR,
            f"_tmp_chunks_{os.path.splitext(os.path.basename(audio_path))[0]}",
        )
        os.makedirs(tmp_dir, exist_ok=True)

        def _split_audio_into_chunks(path: str, chunk_s: int) -> list:
            # Obtener duración total del audio usando ffprobe (ffmpeg must be available)
            try:
                cmd = [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
                total_duration = float(proc.stdout.strip())
            except Exception:
                # fallback: si ffprobe falla, asumimos un solo chunk
                total_duration = chunk_s

            num_chunks = max(1, math.ceil(total_duration / chunk_s))
            chunk_paths = []
            for i in range(num_chunks):
                start = i * chunk_s
                out_path = os.path.join(tmp_dir, f"chunk_{i:04d}.wav")
                # usar ffmpeg para recortar (re-encode a wav 16k mono para compatibilidad)
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    path,
                    "-ss",
                    str(start),
                    "-t",
                    str(chunk_s),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    out_path,
                ]
                try:
                    subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True,
                    )
                    # Verificar archivo generado y su tamaño
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
                        chunk_paths.append(out_path)
                    else:
                        # si el chunk está vacío, lo ignoramos
                        if os.path.exists(out_path):
                            os.remove(out_path)
                except Exception:
                    # en caso de error con ffmpeg, intentar continuar con los chunks ya creados
                    break

            return chunk_paths

        chunks = _split_audio_into_chunks(audio_path, chunk_length_seconds)

        full_text_parts = []
        if tqdm is not None:
            pbar = tqdm(
                total=len(chunks),
                desc=f"Transcribiendo: {os.path.basename(audio_path)}",
                unit="chunk",
            )
        else:
            pbar = None

        try:
            for chunk_path in chunks:
                # transcribir cada fragmento y acumular texto
                res = model.transcribe(chunk_path, language="es")
                txt = res.get("text", "")
                full_text_parts.append(txt.strip())
                if pbar is not None:
                    pbar.update(1)
            if pbar is not None:
                pbar.close()

            transcript = "\n".join([p for p in full_text_parts if p])
            # Guardar la transcripción en un archivo de texto
            with open(output_text_path, "w", encoding="utf-8") as f:
                f.write(transcript)

            print(f"Transcripción guardada en: {output_text_path}")
        finally:
            # Limpiar archivos temporales
            try:
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir)
            except Exception:
                pass
        return

    # Si no se solicita chunking, mantener el comportamiento anterior (barra animada)
    result_holder = {}

    def _worker():
        try:
            result_holder["result"] = model.transcribe(audio_path, language="es")
        except Exception as e:  # store exception to raise later
            result_holder["error"] = e

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    # Mostrar barra de progreso animada si tqdm está disponible
    if tqdm is not None:
        desc = f"Transcribiendo: {os.path.basename(audio_path)}"
        with tqdm(desc=desc, unit="it", leave=True) as pbar:
            while thread.is_alive():
                pbar.update(1)
                time.sleep(0.05)
            # un pequeño avance final para que la barra termine con un número
            pbar.update(1)
    else:
        # Caída a un spinner simple si no está instalado tqdm
        spinner = "|/-\\"
        idx = 0
        while thread.is_alive():
            print(
                f"Transcribiendo... {spinner[idx % len(spinner)]}", end="\r", flush=True
            )
            idx += 1
            time.sleep(0.1)
        # limpiar la línea
        print(" " * 40, end="\r")

    thread.join()

    # Propagar cualquier excepción ocurrida dentro del hilo
    if "error" in result_holder:
        raise result_holder["error"]

    result = result_holder.get("result", {})
    transcript = result.get("text", "")

    # Guardar la transcripción en un archivo de texto
    with open(output_text_path, "w", encoding="utf-8") as f:
        f.write(transcript)

    print(f"Transcripción guardada en: {output_text_path}")


def process_single_media_file(
    file_path: str, model: str, device: str, chunk_length_seconds: int = 0
) -> Tuple[bool, str]:
    """
    Procesa un único archivo de audio o video. Devuelve (success, message).
    """
    try:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        audio_output_path = os.path.join(OUTPUT_DIR, f"{base_name}.wav")
        transcription_output_path = os.path.join(OUTPUT_DIR, f"{base_name}.txt")

        if is_video_file(file_path):
            extract_audio_from_video(file_path, audio_output_path)
            audio_for_transcription = audio_output_path
        elif is_audio_file(file_path):
            audio_for_transcription = file_path
        else:
            return False, f"Tipo de archivo no soportado: {file_path}"

        transcribe_audio(
            audio_for_transcription,
            transcription_output_path,
            model_size=model,
            device=device,
            chunk_length_seconds=chunk_length_seconds,
        )
        return (
            True,
            f"Procesado correctamente: {file_path} -> {transcription_output_path}",
        )
    except Exception as e:
        return False, str(e)


def main():
    global OUTPUT_DIR

    # Cargar variables de entorno desde un posible archivo .env en la raíz
    def load_dotenv(path: str = ".env") -> None:
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

    # Creamos el parser sin el help por defecto para definirlo explícitamente
    parser = argparse.ArgumentParser(
        description="Transcribir grabaciones de reuniones a texto (archivo único o carpeta)",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Ejemplo de uso:\n"
            "  python main.py recording.mp4\n"
            "  python main.py /ruta/a/carpeta_con_medias --model medium --device cpu --output-dir out\n"
        ),
        add_help=False,
    )

    # Añadimos una opción --help en Español (y -h) para mostrar la ayuda y salir
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="Mostrar esta ayuda y salir (también disponible como -h)",
    )

    parser.add_argument(
        "path",
        help="Ruta a un archivo de audio/video o a una carpeta que contiene varios archivos",
    )
    parser.add_argument(
        "--model",
        help="Modelo Whisper a usar (ej: tiny, base, small, medium, large)",
    )
    parser.add_argument(
        "--device",
        choices=["cuda", "cpu"],
        help="Dispositivo para ejecutar (cuda|cpu). Si no se especifica, se detectará automáticamente.",
    )
    parser.add_argument(
        "--output-dir",
        default=OUTPUT_DIR,
        help="Directorio de salida para transcripciones",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=0,
        help="(Opcional) Tamaño en segundos para dividir el audio en fragmentos y transcribir por partes. 0 = desactivar chunking",
    )

    args = parser.parse_args()

    # Crear el directorio de salida si no existe
    OUTPUT = args.output_dir
    OUTPUT_DIR = OUTPUT
    ensure_output_directory()

    # Obtener la ruta desde los argumentos
    in_path = args.path

    if not os.path.exists(in_path):
        print(f"El path '{in_path}' no existe. Por favor verifica la ruta.")
        sys.exit(1)

    # Determinar opciones de modelo y dispositivo: prioridad -> args -> .env -> por defecto
    model = args.model or os.environ.get("MODEL", "small")
    env_device = args.device or os.environ.get("DEVICE")
    if env_device:
        device = env_device
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    # Si es carpeta, procesar todos los archivos compatibles
    if os.path.isdir(in_path):
        files = sorted(os.listdir(in_path))
        if not files:
            print(f"La carpeta '{in_path}' está vacía.")
            sys.exit(1)

        total = 0
        successes = 0
        for fname in files:
            file_path = os.path.join(in_path, fname)
            if not os.path.isfile(file_path):
                continue
            if not (is_audio_file(file_path) or is_video_file(file_path)):
                print(f"Omitiendo (tipo no soportado): {file_path}")
                continue

            total += 1
            ok, msg = process_single_media_file(file_path, model, device, args.chunk)
            if ok:
                successes += 1
                print(msg)
            else:
                print(f"Error procesando '{file_path}': {msg}")

        print(f"Procesados: {successes}/{total} archivos con éxito.")
    else:
        # Es un archivo único
        ok, msg = process_single_media_file(in_path, model, device, args.chunk)
        if not ok:
            print(f"Error: {msg}")
            sys.exit(1)
        print(msg)

    print("¡Proceso completado!")
    print(f"Resultados guardados en la carpeta: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
