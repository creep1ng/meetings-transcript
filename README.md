# Meetings Transcript

Transcribe videos from Amazon S3 to text using OpenAI Whisper. This application downloads videos from S3, extracts audio, transcribes using Whisper, and uploads the resulting text back to S3.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Sequence Diagram](#sequence-diagram)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [CLI Examples](#cli-examples)
- [AWS IAM Permissions](#aws-iam-permissions)
- [Running with LocalStack (Emulator)](#running-with-localstack-emulator)
- [Troubleshooting](#troubleshooting)

---

## Features

- **S3 Integration**: List, download, and upload objects from Amazon S3
- **Whisper Transcription**: Local transcription using OpenAI Whisper model
- **Audio Extraction**: Extract audio from video files using ffmpeg
- **Chunking Support**: Process large files by splitting into chunks
- **Atomic Uploads**: Safe uploads to S3 using temporary keys and copy operations
- **Resilient**: Exponential backoff retries for transient errors
- **Secure**: Credentials never logged; secret redaction in logs

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI (main.py)                           │
│  ┌───────────┐  ┌──────────────┐  ┌─────────────────────────┐  │
│  │   list    │  │   transcribe │  │        download         │  │
│  └───────────┘  └──────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     config.py (Config)                          │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ • AWS credentials  • S3 bucket/prefixes                │    │
│  │ • Model settings   • Timeouts/retries                  │    │
│  │ • Chunk sizes      • Validation limits                  │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────┐     ┌───────────────────────────────┐
│    s3_client.py         │     │      transcribe.py            │
│  ┌───────────────────┐ │     │  ┌─────────────────────────┐  │
│  │ • list_objects    │ │     │  │ TranscriptionService    │  │
│  │ • download_to_*   │ │     │  │  • validate_file        │  │
│  │ • upload_text     │ │     │  │  • extract_audio        │  │
│  │ • atomic uploads │ │     │  │  • transcribe_audio     │  │
│  │ • retries/backoff │ │     │  │  • process_video        │  │
│  └───────────────────┘ │     │  └─────────────────────────┘  │
└─────────────────────────┘     └───────────────────────────────┘
              │                               │
              ▼                               ▼
     ┌────────────────┐              ┌────────────────────┐
     │  Amazon S3     │              │  Whisper (local)   │
     │  Bucket        │              │  Model             │
     └────────────────┘              └────────────────────┘
```

---

## Sequence Diagram

```
participant User
participant CLI
participant Config
participant S3Client
participant TranscriptionService
participant Whisper
participant S3Bucket

User->>CLI: python main.py transcribe videos/meeting.mp4
CLI->>Config: load_config()
Config-->>CLI: Config object
CLI->>S3Client: download_to_file(videos/meeting.mp4)
S3Client->>S3Bucket: GetObject
S3Bucket-->>S3Client: Video stream
S3Client-->>CLI: Local file
CLI->>TranscriptionService: process_video()
TranscriptionService->>TranscriptionService: extract_audio()
TranscriptionService->>Whisper: transcribe(audio)
Whisper-->>TranscriptionService: text
TranscriptionService->>S3Client: upload_text()
S3Client->>S3Bucket: PutObject (temp)
S3Bucket-->>S3Client: ETag
S3Client->>S3Bucket: CopyObject (final)
S3Client->>S3Bucket: DeleteObject (temp)
S3Client-->>CLI: S3 key
CLI-->>User: Done
```

---

## Prerequisites

- Python 3.8+
- AWS credentials or IAM role with S3 access
- ffmpeg (for audio extraction)
- GPU with CUDA (optional, for faster transcription)

---

## Installation

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd meetings-transcript
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/Mac
   # or
   .venv\Scripts\activate     # Windows
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Install ffmpeg** (required for audio extraction):
   ```bash
   # Ubuntu/Debian
   sudo apt-get install ffmpeg
   
   # macOS
   brew install ffmpeg
   
   # Windows
   choco install ffmpeg
   ```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with your configuration:

```env
# AWS Credentials
AWS_ACCESS_KEY_ID=your_access_key_id
AWS_SECRET_ACCESS_KEY=your_secret_access_key
AWS_REGION=us-east-1

# S3 Configuration
S3_BUCKET_NAME=your-bucket-name
VIDEO_PREFIX=videos/
TRANSCRIPTS_PREFIX=transcripts/

# Transcription Settings
MODEL_SIZE=small
DEVICE=cpu
TRANSCRIPT_CHUNK_SECONDS=0
DOWNLOAD_CHUNK_BYTES=10485760

# Timeouts
S3_TIMEOUT=300
TRANSCRIPTION_TIMEOUT=3600
MAX_RETRIES=3
```

---

## Usage

### List available videos

```bash
python main.py list
```

Output:
```
Key                                                         Size      Last Modified
-----------------------------------------------------------------------------------------------
videos/meeting_jan_2024.mp4                               125.43MB  2024-01-15 14:30:00
videos/team_sync.wav                                       45.21MB  2024-01-16 09:15:00

Total: 2 videos
```

### Transcribe a specific video

```bash
python main.py transcribe videos/meeting_jan_2024.mp4
```

### Transcribe all videos

```bash
python main.py transcribe --all
```

### Transcribe with chunking (for large files)

```bash
python main.py transcribe videos/large_meeting.mp4 --transcript-chunk 120
```

### Download a transcription

```bash
python main.py download transcripts/meeting_jan_2024.mp4.txt
```

### Download to specific directory

```bash
python main.py download transcripts/meeting_jan_2024.mp4.txt -o ./output
```

---

## CLI Examples

### Full workflow

```bash
# 1. List available videos
python main.py list

# 2. Transcribe specific video
python main.py transcribe videos/team_meeting.mp4

# 3. Download the transcription
python main.py download transcripts/team_meeting.mp4.txt -o ./downloads
```

### With custom environment file

```bash
python main.py --env production.env list
python main.py --env production.env transcribe videos/meeting.mp4
```

### With verbose logging

```bash
python main.py --log-level DEBUG transcribe videos/meeting.mp4
```

---

## AWS IAM Permissions

Create an IAM policy with these permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket"
            ],
            "Resource": "arn:aws:s3:::YOUR_BUCKET_NAME"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:CopyObject"
            ],
            "Resource": [
                "arn:aws:s3:::YOUR_BUCKET_NAME/videos/*",
                "arn:aws:s3:::YOUR_BUCKET_NAME/transcripts/*"
            ]
        }
    ]
}
```

Attach this policy to a user or role. If running on EC2, use an IAM instance profile instead.

---

## Running with LocalStack (Emulator)

For local development without AWS:

1. **Start LocalStack**:
   ```bash
   docker run -d -p 4566:4566 localstack/localstack
   ```

2. **Create bucket**:
   ```bash
   aws --endpoint-url=http://localhost:4566 s3 mb s3://test-bucket
   ```

3. **Upload test video**:
   ```bash
   aws --endpoint-url=http://localhost:4566 s3 cp video.mp4 s3://test-bucket/videos/
   ```

4. **Configure .env**:
   ```env
   AWS_ACCESS_KEY_ID=test
   AWS_SECRET_ACCESS_KEY=test
   AWS_REGION=us-east-1
   S3_BUCKET_NAME=test-bucket
   ```

5. **Run CLI**:
   ```bash
   python main.py list
   python main.py transcribe videos/video.mp4
   ```

---

## Troubleshooting

### Credential errors

Ensure AWS credentials are set correctly:
```bash
aws configure
# or set environment variables
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
```

### ffmpeg not found

Ensure ffmpeg is in your PATH:
```bash
ffmpeg -version
```

### CUDA out of memory

Use CPU instead:
```bash
DEVICE=cpu python main.py transcribe video.mp4
```

### Large file chunking

For files > 500MB, enable chunking:
```bash
python main.py transcribe large_video.mp4 --transcript-chunk 120
```

---

## License

MIT License
