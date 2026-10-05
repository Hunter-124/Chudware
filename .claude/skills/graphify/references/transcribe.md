# graphify reference: transcribe video and audio

Use after the semantic cache check when uncached original sources are in detect's `video` category (audio is classified there too). Raw media cannot be read as text by extraction subagents. The installed API is `graphify.transcribe.transcribe`; Whisper runs locally on CPU with no API key. It requires the package's video dependencies (`faster-whisper`, and `yt-dlp` for URLs). Report missing dependencies rather than changing this repo's installation or inventing a provider transcription command. The first Whisper model load may download model weights; local computation is not a promise of zero network access.

## Domain hint and model

If a graph exists, use `graphify god-nodes --top 10` to read its hub labels and compose one short domain sentence. No extra LLM/API call is needed. With no relevant graph, use `Use proper punctuation and paragraph breaks.` The default model is `base`; honor a user-selected `--whisper-model` by setting the exported variable below. This is a slash-workflow parameter, not an `extract` CLI option.

## Transcribe only cache misses

```bash
export GRAPHIFY_WHISPER_MODEL=base
export GRAPHIFY_WHISPER_PROMPT="Use proper punctuation and paragraph breaks."
"$(cat graphify-out/.graphify_python)" -c "
import hashlib, json, os
from pathlib import Path
from graphify.transcribe import transcribe
d = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))
unc = set(Path('graphify-out/.graphify_uncached.txt').read_text(encoding='utf-8').splitlines())
media = [f for f in d.get('files', {}).get('video', []) if f in unc]
paths, failed = {}, []
for source in media:
    # Distinct directories prevent equal basenames from sharing transcript text.
    key = hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
    try:
        text = transcribe(source, output_dir=Path('graphify-out/transcripts') / key,
                          initial_prompt=os.environ.get('GRAPHIFY_WHISPER_PROMPT'), force=True)
        paths[source] = str(text.resolve())
    except Exception:
        failed.append(source)
Path('graphify-out/.graphify_transcripts.json').write_text(json.dumps(paths, ensure_ascii=False), encoding='utf-8')
print('Transcribed', len(paths), 'of', len(media), 'media inputs')
if failed:
    raise SystemExit('ERROR: transcription failed for ' + str(len(failed)) + ' inputs; retry before extraction')
"
```

Replace the prompt with the domain hint when available. `force=True` refreshes each **semantic cache miss**: the package's default transcript cache is basename-based and would otherwise replay old text after a media edit. Whisper progress is printed to stdout, so write the JSON mapping from Python, never by redirecting command output.

Pass the resulting mapping as `MEDIA_TRANSCRIPTS` in `extraction-spec.md`. Subagents read transcript text but copy the **original media path** into `source_file` and derive IDs from that original path. This retains source provenance and gives `cache-check`, `save_semantic_cache`, incremental manifests and deletion pruning the same underlying media identity. Failed media must remain unstamped and uncached; retry, or obtain explicit acceptance to retain old graph contributions and continue without those inputs. Do not silently claim every input was transcribed. Preserve transcript text for source review after removing only the temporary mapping file.
