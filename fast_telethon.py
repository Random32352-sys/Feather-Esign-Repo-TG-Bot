import asyncio
import inspect
import logging
from typing import Optional, List, Union, Callable

from telethon import TelegramClient, utils, helpers
from telethon.tl.types import InputFileLocation, InputDocumentFileLocation, InputPhotoFileLocation

logger = logging.getLogger(__name__)

class DownloadSender:
    def __init__(
        self,
        client: TelegramClient,
        file: InputFileLocation,
        file_size: int,
        part_size_kb: Optional[float] = None,
        connection_count: Optional[int] = None,
        part_count_cache: Optional[int] = None,
        dc_id: Optional[int] = None,
        msg_data: Optional[tuple] = None,
    ):
        self.client = client
        self.file = file
        self.file_size = file_size
        self.part_size_kb = part_size_kb
        self.connection_count = connection_count
        self.part_count_cache = part_count_cache
        self.dc_id = dc_id
        self.msg_data = msg_data

async def download_file(
    client: TelegramClient,
    location: Union[InputDocumentFileLocation, InputPhotoFileLocation],
    out: Union[str, callable],
    file_size: int,
    progress_callback: Optional[Callable] = None,
) -> Union[str, bytes]:
    """
    Download a file in parallel using multiple connections.
    """
    size = file_size
    # dc_id = location.dc_id # InputDocumentFileLocation doesn't have dc_id, but we don't strictly need it for GetFileRequest if client handles it
    
    # Calculate optimal part size and connection count
    # 1MB part size for files > 1GB
    part_size_kb = 512
    if size > 1024 * 1024 * 1024:
        part_size_kb = 1024
    
    # Use 4 connections by default, up to 16 for large files
    connection_count = 4
    if size > 100 * 1024 * 1024:
        connection_count = 8
    
    part_size = int(part_size_kb * 1024)
    if part_size % 4096 != 0:
        part_size = (part_size // 4096 + 1) * 4096
    
    logger.info(f"Starting parallel download: size={size/1024/1024:.2f}MB, parts={part_size/1024}KB, connections={connection_count}")
    
    # Create output stream
    if isinstance(out, str):
        f = open(out, 'wb')
        close_file = True
    else:
        f = out
        close_file = False
        
    try:
        # Create separate clients for parallel downloading
        # Note: We reuse the session but create new connections
        # This is a simplified approach; true parallel might need distinct sessions or smart work distribution
        # For now, we use a single client with concurrent chunks which Telethon handles reasonably well
        # if connection settings are optimized.
        
        # ACTUALLY, strict parallel downloads in Telethon require separate clients or deep internal hacking.
        # The provided "FastTelethon" snippet usually involves creating multiple clients.
        # Given we have one session, true multi-connection parallel is tricky without auth issues.
        # However, we can use `client.stream_media` with `asyncio.gather` for chunks if the library permits.
        
        # Let's use the efficient chunked downloader approach which allows
        # fetching non-sequential chunks concurrently if the client supports it.
        
        # We will split the file into chunks and download them concurrently
        chunks = []
        offset = 0
        while offset < size:
            chunks.append((offset, min(part_size, size - offset)))
            offset += part_size
            
        total_parts = len(chunks)
        downloaded_parts = 0
        downloaded_bytes = 0
        
        # Semaphore to limit concurrency
        sem = asyncio.Semaphore(connection_count)
        
        async def download_chunk(offset, length):
            nonlocal downloaded_bytes
            async with sem:
                result = await client(helpers.functions.upload.GetFileRequest(
                    location=location,
                    offset=offset,
                    limit=length
                ))
                return offset, result.bytes

        # Create tasks
        tasks = [download_chunk(off, len) for off, len in chunks]
        
        # Process as they complete to write to file in order?
        # No, for speed we should write as matches come in, but seeking is needed.
        # Since we opened with 'wb', we can use seek() if it's a file object.
        
        if hasattr(f, 'seek'):
            pending_tasks = set(tasks)
            # Use as_completed to update progress efficiently
            for future in asyncio.as_completed(tasks):
                try:
                    offset, data = await future
                    f.seek(offset)
                    f.write(data)
                    
                    downloaded_bytes += len(data)
                    if progress_callback:
                        if inspect.iscoroutinefunction(progress_callback):
                            await progress_callback(downloaded_bytes, size)
                        else:
                            progress_callback(downloaded_bytes, size)
                except Exception as e:
                    logger.error(f"Chunk download failed: {e}")
                    raise e
        else:
            # If not seekable (pipe/socket), we must gather all (high memory usage!) or wait sequentially
            # Assuming file object for 'wb' is seekable.
            logger.warning("Output file is not seekable, falling back to sequential download to ensure order (slower)")
            async for chunk in client.iter_download(location, chunk_size=part_size):
                f.write(chunk)
                downloaded_bytes += len(chunk)
                if progress_callback:
                    if inspect.iscoroutinefunction(progress_callback):
                        await progress_callback(downloaded_bytes, size)
                    else:
                        progress_callback(downloaded_bytes, size)

    finally:
        if close_file:
            f.close()

    return out
