from __future__ import annotations

import getpass
import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/135 Safari/537.36"
PASSPORT = "https://passport.pku.edu.cn"
IAAA = "https://iaaa.pku.edu.cn/iaaa"
VERBOSE = False

PLAYBACK_STATUS_LABELS = {
    "1": "live",
    "2": "not started",
    "3": "replay generating",
    "4": "no access",
    "5": "replay unavailable",
    "6": "replay available",
}


def debug(*args, **kwargs) -> None:
    if VERBOSE:
        print(*args, **kwargs)


class HiddenInputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        data = dict(attrs)
        name = data.get("name")
        if name:
            self.values[name] = data.get("value") or ""


def response_shape(body: str) -> str:
    try:
        value = json.loads(body)
    except Exception:
        return "non-JSON"
    if isinstance(value, dict):
        return "{" + ", ".join(sorted(value)) + "}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return type(value).__name__


def show_response(response: requests.Response, body: str = "") -> None:
    debug(f"HTTP {response.status_code} {response.reason}")
    if response.headers.get("Location"):
        debug("Location:", response.headers["Location"])
    cookie_names = []
    for header in response.raw.headers.get_all("Set-Cookie") or []:
        cookie_names.append(header.split("=", 1)[0])
    if cookie_names:
        debug("Set-Cookie names:", ", ".join(sorted(set(cookie_names))))
    if body:
        debug("Response shape:", response_shape(body))
        try:
            value = json.loads(body)
            if isinstance(value, dict) and "success" in value:
                debug("success:", value["success"])
            for field in ("err", "errMsg", "message", "code"):
                if isinstance(value, dict) and field in value:
                    debug(f"{field}:", str(value[field])[:300])
            if isinstance(value, dict) and "errors" in value:
                debug("errors shape:", response_shape(json.dumps(value["errors"])))
        except Exception:
            compact = re.sub(r"\s+", " ", body).strip()
            compact = re.sub(
                r"(?i)(password|token|userName|cookie|access_token)\s*[=:]\s*[\"']?[^&\s\"']+",
                r"\1=<redacted>",
                compact,
            )
            debug("Body preview:", compact[:240])


def show_callback_clues(body: str) -> None:
    debug("Callback script/src clues:")
    found = 0
    for line in body.splitlines():
        if re.search(r"(?i)(script|bridge|PKULoginSuccess|userInfo|userinfo|_token|token|redirect|json)", line):
            compact = re.sub(r"\s+", " ", line).strip()
            compact = re.sub(
                r"(?i)(password|token|userName|cookie|access_token|_token2?)\s*[=:]\s*[\"']?[^&\s\"'<>]+",
                r"\1=<redacted>",
                compact,
            )
            debug(" ", compact[:500])
            found += 1
            if found >= 40:
                debug("  ... output capped at 40 lines")
                break
    if found == 0:
        debug("  none found in inline HTML")
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.I | re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    visible = re.sub(r"\s+", " ", visible).strip()
    if visible:
        debug("Callback visible text:", visible[:300])


def request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault("User-Agent", UA)
    return session.request(method, url, timeout=30, allow_redirects=False, **kwargs)


def api_sign(params: dict[str, str]) -> str:
    material = "".join(key + params[key] for key in sorted(params))
    return hashlib.md5((material + "8225ca5fa4d56d6fde540db6024fba39").encode()).hexdigest()


def course_api(session: requests.Session, path: str, params: dict[str, str], api_token: str) -> requests.Response:
    signed = {key: value for key, value in params.items() if value != ""}
    signed.update({
        "tenantid": "d438cafb4c34b3b49b3a523bbef50203",
        "tenantId": "d438cafb4c34b3b49b3a523bbef50203",
        "api_version": "3.12.0",
        "app_id": "50",
        "client": "android",
    })
    signed["sign"] = api_sign(signed)
    return request(
        session,
        "GET",
        "https://yjapise.pku.edu.cn/courseapi/" + path,
        params=signed,
        headers={"token": api_token, "urlname": "courseapi"},
    )


def json_data(response: requests.Response):
    try:
        value = response.json()
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("success") is False:
        debug("API error:", value.get("err"), str(value.get("errMsg", ""))[:200])
    return value.get("data") if isinstance(value, dict) else value


def print_course_records(data) -> None:
    records = data.get("lists", []) if isinstance(data, dict) else []
    print(f"Course records: {len(records)}")
    for index, item in enumerate(records, 1):
        if not isinstance(item, dict):
            continue
        print(
            f"  [{index}] course_id={item.get('course_id')}, "
            f"course_name={item.get('course_name')}, "
            f"teacher={item.get('course_teacher')}, "
            f"college={item.get('course_college')}, "
            f"term={item.get('course_term')}"
        )


def print_date_records(records: list[dict]) -> None:
    print(f"Date records: {len(records)}")
    for index, item in enumerate(records, 1):
        status = str(item.get("sub_status", ""))
        print(
            f"  [{index}] course={item.get('course_name')}, "
            f"lesson={item.get('sub_name')}, teacher={item.get('course_teacher')}, "
            f"room={item.get('room_name')}, start={item.get('start_at')}, "
            f"end={item.get('end_at')}, status={status} "
            f"({PLAYBACK_STATUS_LABELS.get(status, 'unknown')})"
        )


def select_record_value(records: list, prompt: str, id_key: str) -> str:
    """Accept either the displayed 1-based index or the record's existing ID."""
    if not isinstance(records, list):
        print(f"No selectable records for {id_key}.")
        return ""
    choice = input(prompt).strip()
    if not choice:
        return ""
    # Prefer an exact ID match so numeric IDs are not mistaken for list indexes.
    for item in records:
        if isinstance(item, dict) and str(item.get(id_key, "")).strip() == choice:
            return choice
    try:
        index = int(choice)
    except ValueError:
        index = 0
    if 1 <= index <= len(records) and isinstance(records[index - 1], dict):
        value = records[index - 1].get(id_key)
        if value is not None and str(value).strip():
            return str(value).strip()
    print(f"No matching {id_key}: {choice}")
    return ""


def select_record_by_index(records: list[dict], prompt: str) -> dict | None:
    choice = input(prompt).strip()
    if not choice:
        return None
    try:
        index = int(choice)
    except ValueError:
        index = 0
    if 1 <= index <= len(records):
        return records[index - 1]
    print(f"No matching record: {choice}")
    return None


def print_sub_records(data) -> None:
    # get-course-detail maps to PlaybackVideoListBean.Data, whose subject
    # collection is named sub_list (the course-list endpoint uses lists).
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    records = data.get("sub_list", []) if isinstance(data, dict) else []
    print(f"Subject records: {len(records)}")
    for index, item in enumerate(records, 1):
        if not isinstance(item, dict):
            continue
        print(
            f"  [{index}] sub_id={item.get('id')}, title={item.get('sub_title')}, "
            f"status={item.get('sub_status')}, begin={item.get('class_begin')}, "
            f"over={item.get('class_over')}, playback_status={item.get('playback_status')}"
        )
    if not records and isinstance(data, dict):
        print("Subject response keys:", ", ".join(sorted(data)))


def print_video_info(data) -> str:
    if not isinstance(data, dict):
        debug("Video info data shape:", response_shape(json.dumps(data)))
        return ""
    video_data = data.get("data", data)
    if not isinstance(video_data, dict):
        debug("Video info data shape:", response_shape(json.dumps(video_data)))
        return ""
    debug("Video info keys:", ", ".join(sorted(video_data)))
    choices = video_data.get("videoArr") or []
    media_url = ""
    debug(f"Video choices: {len(choices)}")
    for item in choices:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("path", ""))
        if not media_url and raw_url:
            media_url = raw_url
        parsed = urlsplit(raw_url)
        debug(
            f"  tag={item.get('tag')}, select={item.get('select')}, "
            f"file_size={item.get('file_size')}, scheme={parsed.scheme}, "
            f"host={parsed.netloc}, path={parsed.path}, query_keys="
            f"{','.join(sorted({part.split('=', 1)[0] for part in parsed.query.split('&') if part}))}"
        )
    sub_content = video_data.get("sub_content") or {}
    if isinstance(sub_content, dict):
        saved = sub_content.get("save_playback") or {}
        if isinstance(saved, dict) and saved.get("contents"):
            parsed = urlsplit(str(saved["contents"]))
            debug(
                "Fallback save_playback.contents: "
                f"scheme={parsed.scheme}, host={parsed.netloc}, path={parsed.path}"
            )
            if not media_url:
                media_url = str(saved["contents"])
    return media_url


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value or "replay"


def playlist_duration(body: str) -> float:
    return sum(float(match) for match in re.findall(r"#EXTINF:([0-9]+(?:\.[0-9]+)?)", body))


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def run_ffmpeg_with_progress(command: list[str], duration: float) -> int:
    command = [*command[: command.index("-i")], "-progress", "pipe:1", "-nostats", *command[command.index("-i"):]]
    stderr = None if VERBOSE else subprocess.DEVNULL
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1)
    current = 0.0
    started_at = time.monotonic()
    if process.stdout:
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_ms":
                current = int(value or 0) / 1_000_000
                elapsed = time.monotonic() - started_at
                if duration > 0:
                    ratio = min(current / duration, 1.0)
                    width = 32
                    bar = "#" * int(width * ratio) + "-" * (width - int(width * ratio))
                    eta = elapsed * (duration / current - 1) if current > 0 else 0
                    print(
                        f"\rffmpeg [{bar}] {ratio * 100:5.1f}% "
                        f"processed {format_time(current)}/{format_time(duration)} "
                        f"elapsed {format_time(elapsed)} ETA {format_time(eta)}",
                        end="", flush=True,
                    )
                else:
                    print(
                        f"\rffmpeg processed {format_time(current)} "
                        f"elapsed {format_time(elapsed)}",
                        end="", flush=True,
                    )
    code = process.wait()
    print()
    return code


def playlist_identity(body: str, playlist_url: str) -> str:
    """Return an identity that survives changes to expiring URL query strings."""
    normalized: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = re.sub(
                r'URI="([^"]+)"',
                lambda match: 'URI="' + urlunsplit(
                    (*urlsplit(urljoin(playlist_url, match.group(1)))[:3], "", "")
                ) + '"',
                line,
            )
            normalized.append(line)
            continue
        absolute = urlsplit(urljoin(playlist_url, line))
        normalized.append(urlunsplit((absolute.scheme, absolute.netloc, absolute.path, "", "")))
    return hashlib.sha256("\n".join(normalized).encode()).hexdigest()


def fetch_media_playlist(
    session: requests.Session, playlist_url: str, headers: dict[str, str], depth: int = 0,
) -> tuple[str, str]:
    """Resolve a master playlist to its highest-bandwidth media playlist."""
    if depth > 5:
        raise RuntimeError("HLS playlist nesting is too deep")
    response = session.get(
        playlist_url, headers={"User-Agent": UA, **headers}, timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()
    body = response.text
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    variants: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        match = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line)
        bandwidth = int(match.group(1)) if match else 0
        if index + 1 < len(lines) and not lines[index + 1].startswith("#"):
            variants.append((bandwidth, urljoin(response.url, lines[index + 1])))
    if variants:
        _, selected_url = max(variants, key=lambda item: item[0])
        debug(f"Selected HLS variant from {len(variants)} choices")
        return fetch_media_playlist(session, selected_url, headers, depth + 1)
    return response.url, body


def download_cached_resource(
    session: requests.Session, url: str, destination: Path, headers: dict[str, str],
) -> bool:
    """Download one resource atomically. Return False when it was cached."""
    if destination.is_file() and destination.stat().st_size > 0:
        return False
    temporary = destination.with_name(destination.name + ".part")
    try:
        with session.get(
            url, headers={"User-Agent": UA, **headers}, timeout=30,
            allow_redirects=True, stream=True,
        ) as response:
            response.raise_for_status()
            expected = int(response.headers.get("Content-Length", "0") or 0)
            written = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        output.write(chunk)
                        written += len(chunk)
        if written == 0 or (expected and written != expected):
            raise IOError(f"incomplete response: expected {expected}, received {written}")
        temporary.replace(destination)
        return True
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def cache_hls_playlist(
    session: requests.Session, playlist_url: str, body: str, cache_dir: Path,
    headers: dict[str, str],
) -> tuple[Path, int, int]:
    """Cache HLS resources and write a playlist that refers only to local files."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    identity_file = cache_dir / "playlist.sha256"
    identity = playlist_identity(body, playlist_url)
    if identity_file.exists() and identity_file.read_text(encoding="ascii").strip() != identity:
        raise RuntimeError(
            f"playlist differs from the cache in {cache_dir}; choose another output "
            "name or remove that cache directory"
        )

    identity_file.write_text(identity + "\n", encoding="ascii")

    lines = body.splitlines()
    segment_urls = []
    segment_durations = []
    pending_duration = 0.0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            match = re.match(r"#EXTINF:([0-9]+(?:\.[0-9]+)?)", stripped)
            pending_duration = float(match.group(1)) if match else 0.0
        elif stripped and not stripped.startswith("#"):
            segment_urls.append(urljoin(playlist_url, stripped))
            segment_durations.append(pending_duration)
            pending_duration = 0.0
    total = len(segment_urls)
    if not total:
        raise RuntimeError("media playlist contains no segments")

    local_lines: list[str] = []
    downloaded = 0
    ready_duration = 0.0
    total_duration = sum(segment_durations)
    started_at = time.monotonic()
    segment_index = 0
    auxiliary_names: dict[str, str] = {}
    segment_names: dict[str, str] = {}

    def cache_auxiliary(remote_url: str, kind: str) -> str:
        nonlocal downloaded
        absolute_url = urljoin(playlist_url, remote_url)
        if absolute_url not in auxiliary_names:
            suffix = Path(urlsplit(absolute_url).path).suffix or ".bin"
            name = f"{kind}_{len(auxiliary_names) + 1:03d}{suffix}"
            if download_cached_resource(session, absolute_url, cache_dir / name, headers):
                downloaded += 1
            auxiliary_names[absolute_url] = name
        return auxiliary_names[absolute_url]

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY:") and 'URI="' in line:
            line = re.sub(
                r'URI="([^"]+)"',
                lambda match: f'URI="{cache_auxiliary(match.group(1), "key")}"',
                line,
            )
        elif line.startswith("#EXT-X-MAP:") and 'URI="' in line:
            line = re.sub(
                r'URI="([^"]+)"',
                lambda match: f'URI="{cache_auxiliary(match.group(1), "init")}"',
                line,
            )
        elif not line.startswith("#"):
            segment_index += 1
            remote_url = urljoin(playlist_url, line)
            local_name = segment_names.get(remote_url)
            was_downloaded = False
            if local_name is None:
                suffix = Path(urlsplit(remote_url).path).suffix or ".ts"
                local_name = f"segment_{segment_index:06d}{suffix}"
                segment_names[remote_url] = local_name
                was_downloaded = download_cached_resource(
                    session, remote_url, cache_dir / local_name, headers,
                )
            if was_downloaded:
                downloaded += 1
            ready_duration += segment_durations[segment_index - 1]
            state = "downloaded" if was_downloaded else "cached"
            elapsed = time.monotonic() - started_at
            eta = (
                elapsed * (total_duration / ready_duration - 1)
                if ready_duration > 0 else 0
            )
            print(
                f"\rSegments: {segment_index}/{total} ({state}) "
                f"ready {format_time(ready_duration)}/{format_time(total_duration)} "
                f"elapsed {format_time(elapsed)} ETA {format_time(eta)}",
                end="", flush=True,
            )
            line = local_name
        local_lines.append(line)
    print()

    local_playlist = cache_dir / "local.m3u8"
    local_playlist.write_text("\n".join(local_lines) + "\n", encoding="utf-8")
    return local_playlist, downloaded, total


def resumable_hls_download(
    session: requests.Session, media_url: str, media_cookie: str,
    output_path: Path, ffmpeg: str,
) -> None:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = output_path.parent / f"{output_path.name}.segments"
    headers = {"Cookie": f"_token={media_cookie}"}
    playlist_url, body = fetch_media_playlist(session, media_url, headers)
    duration = playlist_duration(body)
    local_playlist, downloaded, total = cache_hls_playlist(
        session, playlist_url, body, cache_dir, headers,
    )
    print(f"Segments ready: {total}; downloaded now: {downloaded}; cache: {cache_dir}")

    temporary_output = output_path.with_name(
        output_path.stem + ".part" + (output_path.suffix or ".mp4")
    )
    command = [
        ffmpeg, "-hide_banner", "-y", "-protocol_whitelist", "file,crypto,data",
        "-allowed_extensions", "ALL",
        "-i", str(local_playlist), "-c", "copy", str(temporary_output),
    ]
    print("Joining cached segments with ffmpeg.")
    result_code = run_ffmpeg_with_progress(command, duration)
    print("ffmpeg exit code:", result_code)
    if result_code != 0:
        temporary_output.unlink(missing_ok=True)
        print("Join failed; cached segments were kept for the next run.")
        return
    temporary_output.replace(output_path)
    shutil.rmtree(cache_dir)
    print("Saved:", output_path)
    print("Segment cache removed.")


def fetch_all_courses(session: requests.Session, search: str, api_token: str) -> list[dict]:
    """Fetch all pages, using the app's full-page load-more fallback."""
    records: list[dict] = []
    seen_ids: set[str] = set()
    page = 1
    while True:
        response = course_api(
            session,
            "v3/course/get-use-member",
            {"page": str(page), "search": search},
            api_token,
        )
        data = json_data(response)
        page_records = data.get("lists", []) if isinstance(data, dict) else []
        if not isinstance(page_records, list):
            page_records = []
        added = 0
        for item in page_records:
            if not isinstance(item, dict):
                continue
            course_id = str(item.get("course_id", ""))
            if course_id and course_id not in seen_ids:
                seen_ids.add(course_id)
                records.append(item)
                added += 1
        has_next = bool(data.get("hasNextPage")) if isinstance(data, dict) else False
        page_size_value = data.get("pageSize") if isinstance(data, dict) else None
        try:
            page_size = int(page_size_value)
        except (TypeError, ValueError):
            page_size = 10
        # The APK requests the next page when the current page is full.  The
        # server's hasNextPage/count values are stale for this endpoint, so a
        # full page must override hasNextPage=False.
        full_page = len(page_records) >= max(page_size, 10)
        debug(
            f"Course page {page}: received={len(page_records)}, added={added}, "
            f"pageSize={page_size}, hasNextPage={has_next}, full-page-fallback={full_page}"
        )
        if not page_records or added == 0 or not (has_next or full_page):
            break
        page += 1
    return records


def fetch_all_lessons_by_date(
    session: requests.Session, course_date: str, api_token: str,
) -> list[dict]:
    """Fetch every replay record exposed by the app's date-search endpoint."""
    records: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    page = 1
    while True:
        response = course_api(
            session,
            "v3/course/get-use-member-sub",
            {"course_time": course_date, "page": str(page)},
            api_token,
        )
        data = json_data(response)
        container = data.get("data", data) if isinstance(data, dict) else {}
        page_records = container.get("lists", []) if isinstance(container, dict) else []
        if not isinstance(page_records, list):
            page_records = []

        added = 0
        for item in page_records:
            if not isinstance(item, dict):
                continue
            identity = (
                str(item.get("course_id", "")),
                str(item.get("sub_id", "")),
                str(item.get("start_at", "")),
            )
            if identity not in seen:
                seen.add(identity)
                records.append(item)
                added += 1

        has_next_value = data.get("hasNextPage") if isinstance(data, dict) else None
        if has_next_value is None and isinstance(container, dict):
            has_next_value = container.get("hasNextPage")
        has_next = bool(has_next_value)
        page_size_value = container.get("pageSize") if isinstance(container, dict) else None
        try:
            page_size = int(page_size_value)
        except (TypeError, ValueError):
            page_size = 10
        full_page = len(page_records) >= max(page_size, 10)
        debug(
            f"Date page {page}: received={len(page_records)}, added={added}, "
            f"pageSize={page_size}, hasNextPage={has_next}, "
            f"full-page-fallback={full_page}"
        )
        if not page_records or added == 0 or not (has_next or full_page):
            break
        page += 1
    return records


def offer_subject_download(
    session: requests.Session, api_token: str, media_cookie: str,
    course_id: str, subject_id: str, course_name: str = "",
    replay_title: str = "",
) -> None:
    response = course_api(
        session,
        "v3/course/get-course-sub-detail",
        {"course_id": course_id, "sub_id": subject_id},
        api_token,
    )
    show_response(response, response.text)
    video_data = json_data(response)
    media_url = print_video_info(video_data)
    video_record = video_data.get("data", video_data) if isinstance(video_data, dict) else {}
    if isinstance(video_record, dict):
        course_name = str(video_record.get("course_name") or course_name)
        replay_title = str(video_record.get("title") or replay_title)
    default_name = safe_filename(
        f"{course_name or course_id}_{replay_title or subject_id}"
    ) + ".mp4"
    debug("Media Cookie available:", "yes (value hidden)" if media_cookie else "no")
    probe_and_optionally_download(session, media_url, media_cookie, default_name)


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="Debug PKU playback login and download replay")
    parser.add_argument("-v", "--verbose", action="store_true", help="show HTTP/API/ffmpeg debug output")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose
    session = requests.Session()

    debug("== 1. Passport entry ==")
    response = request(
        session,
        "GET",
        PASSPORT + "/auth/login?redirect=" + PASSPORT + "/auth/app-login",
    )
    show_response(response, response.text)
    parser = HiddenInputs()
    parser.feed(response.text)
    oauth_form = {
        "appName": parser.values.get("appName", ""),
        "appID": parser.values.get("appID", ""),
        "redirectUrl": parser.values.get("redirectUrl", PASSPORT),
    }
    debug(f"IAAA form: appID={oauth_form['appID']}, redirectUrl={oauth_form['redirectUrl']}")

    debug("== 2. IAAA OAuth page ==")
    response = request(session, "POST", IAAA + "/oauth.jsp", data=oauth_form)
    show_response(response, response.text)
    parser = HiddenInputs()
    parser.feed(response.text)
    appid = parser.values.get("appid") or oauth_form["appID"]

    debug("== 3. IAAA public key ==")
    response = request(session, "GET", IAAA + "/getPublicKey.do")
    show_response(response, response.text)
    key_data = response.json()
    public_key = RSA.import_key(key_data["key"])

    account = input("Account: ")
    password = getpass.getpass("Password: ")
    encrypted = PKCS1_v1_5.new(public_key).encrypt(password.encode("utf-8"))
    password = ""

    debug("== 4. IAAA login (credentials are not printed) ==")
    login_form = {
        "appid": appid,
        "userName": account,
        "password": base64.b64encode(encrypted).decode("ascii"),
        "randCode": "",
        "smsCode": "",
        "otpCode": "",
        "remTrustChk": "false",
        "redirUrl": oauth_form["redirectUrl"],
    }
    response = request(session, "POST", IAAA + "/oauthlogin.do", data=login_form)
    body = response.text
    show_response(response, body)
    result = response.json()
    if not result.get("success"):
        print("Login did not succeed. Paste this output only.")
        return 2

    token = str(result.get("token", ""))
    debug(f"IAAA token received: yes (length {len(token)}, value hidden)")

    callback = PASSPORT + "/?_rand=debug&token=" + quote(token, safe="")
    debug("== 5. Passport token callback ==")
    response = request(session, "GET", callback)
    callback_body = response.text
    show_response(response, callback_body)
    debug("Callback contains PKULoginSuccess:", "PKULoginSuccess" in callback_body)
    debug("Final session cookie metadata:")
    for cookie in sorted(session.cookies, key=lambda c: c.name):
            debug(
            f"  {cookie.name}: length={len(cookie.value)}, "
            f"domain={cookie.domain}, path={cookie.path}, secure={cookie.secure}"
        )
    show_callback_clues(callback_body)
    debug("== 6. Authenticated course API probe ==")
    api_params = {
        "page": "1",
        "tenantid": "d438cafb4c34b3b49b3a523bbef50203",
        "tenantId": "d438cafb4c34b3b49b3a523bbef50203",
        "api_version": "3.12.0",
        "app_id": "50",
        "client": "android",
    }
    api_params["sign"] = api_sign(api_params)
    for cookie_name in ("_token", "_token2"):
        api_token = next((c.value for c in session.cookies if c.name == cookie_name), "")
        response = request(
            session,
            "GET",
            "https://yjapise.pku.edu.cn/courseapi/v3/course/get-use-member",
            params=api_params,
            headers={"token": api_token, "urlname": "courseapi"},
        )
        debug(f"Token probe: {cookie_name} (value hidden)")
        show_response(response, response.text)

    api_token = next((c.value for c in session.cookies if c.name == "_token2"), "")
    media_cookie = next((c.value for c in session.cookies if c.name == "_token"), "")
    debug("== 7. Course and playback probe ==")
    find_mode = input("Find replay by [1] course or [2] date : ").strip().lower()
    if find_mode in {"2", "d", "date"}:
        default_date = (date.today() - timedelta(days=1)).isoformat()
        course_date = input(f"Course date YYYY-MM-DD [{default_date}]: ").strip() or default_date
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", course_date):
                raise ValueError
            course_date = date.fromisoformat(course_date).isoformat()
        except ValueError:
            print(f"Invalid date: {course_date}. Expected YYYY-MM-DD.")
            return 2
        date_records = fetch_all_lessons_by_date(session, course_date, api_token)
        print_date_records(date_records)
        selected = select_record_by_index(
            date_records,
            "Record number to inspect (empty to stop): ",
        )
        if selected:
            status = str(selected.get("sub_status", ""))
            if status != "6":
                print(
                    "This lesson cannot be opened as a replay: "
                    f"status={status} ({PLAYBACK_STATUS_LABELS.get(status, 'unknown')})."
                )
            else:
                offer_subject_download(
                    session,
                    api_token,
                    media_cookie,
                    str(selected.get("course_id", "")),
                    str(selected.get("sub_id", "")),
                    str(selected.get("course_name", "")),
                    str(selected.get("sub_name", "")),
                )
    else:
        search = input("Course search (empty for all): ")
        course_records = fetch_all_courses(session, search, api_token)
        print_course_records({"lists": course_records})
        course_id = select_record_value(
            course_records,
            "Course number or course_id to inspect (empty to stop): ",
            "course_id",
        )
        if course_id:
            response = course_api(
                session,
                "v3/course/get-course-detail",
                {"course_id": course_id},
                api_token,
            )
            show_response(response, response.text)
            subject_data = json_data(response)
            print_sub_records(subject_data)
            subject_container = subject_data
            if isinstance(subject_container, dict) and isinstance(subject_container.get("data"), dict):
                subject_container = subject_container["data"]
            subjects = subject_container.get("sub_list", []) if isinstance(subject_container, dict) else []
            if not isinstance(subjects, list):
                subjects = []
            subject_id = select_record_value(
                subjects,
                "Lesson number or sub_id to inspect (empty to stop): ",
                "id",
            )
            if subject_id:
                replay_title = next(
                    (str(item.get("sub_title") or "") for item in subjects
                     if isinstance(item, dict) and str(item.get("id")) == subject_id),
                    "",
                )
                selected_course_name = next(
                    (str(item.get("course_name") or "") for item in course_records
                     if isinstance(item, dict) and str(item.get("course_id")) == course_id),
                    "",
                )
                offer_subject_download(
                    session,
                    api_token,
                    media_cookie,
                    course_id,
                    subject_id,
                    selected_course_name,
                    replay_title,
                )
    print("Done. No password, token, or cookie value was printed.")
    return 0


def probe_and_optionally_download(
    session: requests.Session, media_url: str, media_cookie: str,
    default_name: str = "replay.mp4", duration: float = 0.0,
) -> None:
    if not media_url:
        print("No media URL returned.")
        return
    debug("== 8. HLS probe/download ==")
    response = request(
        session,
        "GET",
        media_url,
        headers={"Cookie": f"_token={media_cookie}"},
    )
    debug("Playlist HTTP:", response.status_code, response.reason)
    debug("Playlist Content-Type:", response.headers.get("Content-Type", ""))
    if response.ok:
        lines = [line.strip() for line in response.text.splitlines() if line.strip()]
        duration = duration or playlist_duration(response.text)
        debug("Playlist lines:", len(lines), "segment/child references:", sum(not line.startswith("#") for line in lines))
        tags = [line for line in lines if line.startswith("#")][:8]
        debug("Playlist first tags:", " | ".join(tags))
    else:
        print("Playlist request failed; download skipped.")
        return
    answer = input("Download/resume this replay? [y/N]: ").strip().lower()
    if answer != "y":
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found in PATH.")
        return
    output = input(f"Output file [{default_name}]: ").strip() or default_name
    output_path = Path(output).expanduser()
    resumable_hls_download(session, media_url, media_cookie, output_path, ffmpeg)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as exc:
        print(f"Network error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
