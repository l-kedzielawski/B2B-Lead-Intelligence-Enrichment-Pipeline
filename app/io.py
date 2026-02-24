"""CSV I/O module with auto-detection of delimiters and encoding."""

import csv
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
import pandas as pd
from charset_normalizer import detect
from tqdm import tqdm

logger = logging.getLogger(__name__)


def detect_encoding(file_path: Path) -> str:
    """Detect file encoding using charset-normalizer."""
    try:
        with open(file_path, 'rb') as f:
            raw_data = f.read(100000)  # Read first 100KB
            result = detect(raw_data)
            encoding = result.get('encoding') or 'utf-8'
            confidence = result.get('confidence') or 0
            
            if confidence < 0.5:
                logger.warning(f"Low confidence ({confidence:.2f}) for encoding detection of {file_path}")
                return 'utf-8'
            
            logger.info(f"Detected encoding for {file_path}: {encoding} (confidence: {confidence:.2f})")
            return encoding
    except Exception as e:
        logger.warning(f"Could not detect encoding for {file_path}: {e}. Using utf-8.")
        return 'utf-8'


def detect_delimiter(file_path: Path, encoding: str) -> str:
    """Detect CSV delimiter using csv.Sniffer."""
    try:
        with open(file_path, 'r', encoding=encoding, errors='replace') as f:
            # Read first few lines for sniffing
            sample = f.read(8192)
            if not sample:
                return ','
            
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample)
            delimiter = dialect.delimiter
            
            logger.info(f"Detected delimiter for {file_path}: '{delimiter}'")
            return delimiter
    except Exception as e:
        logger.warning(f"Could not detect delimiter for {file_path}: {e}. Using comma.")
        return ','


def read_csv_file(file_path: Path) -> Tuple[pd.DataFrame, str, str]:
    """
    Read a CSV file with auto-detected encoding and delimiter.
    
    Returns:
        Tuple of (DataFrame, detected_encoding, detected_delimiter)
    """
    file_path = Path(file_path)
    
    if not file_path.exists():
        raise FileNotFoundError(f"CSV file not found: {file_path}")
    
    logger.info(f"Reading CSV: {file_path}")
    
    # Detect encoding and delimiter
    encoding = detect_encoding(file_path)
    delimiter = detect_delimiter(file_path, encoding)
    
    try:
        # Read with pandas
        df = pd.read_csv(
            file_path,
            delimiter=delimiter,
            encoding=encoding,
            dtype=str,  # Read all as strings initially
            keep_default_na=False,
            na_values=[
                '', 'NULL', 'null', 'N/A', 'n/a', '-', 'NA', 'na',
                'k.A.', 'k.a.',     # German (keine Angabe)
                'n/v', 'n.v.t.',    # Dutch (niet van toepassing)
                'n/d', 'n.d.',      # Italian/Spanish (no data)
                's/d',              # Spanish (sin datos)
                'brak',             # Polish (none/missing)
                'sem dados',        # Portuguese
                'keine Angabe',     # German full form
                'nicht verfügbar',  # German (not available)
                'non disponibile',  # Italian
                'no disponible',    # Spanish
                'niet beschikbaar', # Dutch
            ],
            on_bad_lines='warn'
        )
        
        logger.info(f"Successfully read {len(df)} rows from {file_path.name}")
        return df, encoding, delimiter
        
    except Exception as e:
        logger.error(f"Error reading {file_path}: {e}")
        raise


def read_all_csvs(input_dir: Path) -> List[Tuple[str, pd.DataFrame]]:
    """
    Read all CSV files from input directory.
    
    Returns:
        List of tuples (filename, DataFrame)
    """
    input_dir = Path(input_dir)
    
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    
    csv_files = list(input_dir.glob("*.csv"))
    
    if not csv_files:
        raise ValueError(f"No CSV files found in {input_dir}")
    
    logger.info(f"Found {len(csv_files)} CSV files to process")
    
    results = []
    for csv_file in tqdm(csv_files, desc="Reading CSV files"):
        try:
            df, encoding, delimiter = read_csv_file(csv_file)
            results.append((csv_file.name, df))
        except Exception as e:
            logger.error(f"Failed to read {csv_file}: {e}")
            continue
    
    return results


def write_csv_output(df: pd.DataFrame, output_path: Path) -> None:
    """Write DataFrame to CSV file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    df.to_csv(output_path, index=False, encoding='utf-8-sig')  # BOM for Windows/EU Excel compatibility
    logger.info(f"Written CSV output: {output_path}")


def _sanitize_for_excel(value: Any) -> Any:
    """Sanitize string values for Excel compatibility.
    
    Removes control characters and fixes encoding issues that cause
    IllegalCharacterError in openpyxl.
    """
    if pd.isna(value) or value is None:
        return value
    
    if not isinstance(value, str):
        value = str(value)
    
    # Remove control characters (0x00-0x1F, except tab, newline, carriage return)
    # and other problematic characters
    sanitized = []
    for char in value:
        code = ord(char)
        # Allow: tab (9), newline (10), carriage return (13), and printable chars (32-126, 128+)
        # Remove: control chars 0-8, 11-12, 14-31, and 127 (DEL)
        if code in (9, 10, 13) or (32 <= code <= 126) or code >= 128:
            sanitized.append(char)
        else:
            # Replace control chars with space or remove them
            if code == 0:  # Null byte
                continue
            sanitized.append(' ')
    
    # Truncate to Excel limit
    result = ''.join(sanitized)[:32760]
    
    return result if result else value[:32760]


def write_excel_output(df: pd.DataFrame, output_path: Path) -> None:
    """Write DataFrame to Excel file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Sanitize and truncate strings for Excel compatibility
    df_excel = df.copy()
    for col in df_excel.columns:
        if df_excel[col].dtype == 'object':
            df_excel[col] = df_excel[col].apply(_sanitize_for_excel)
    
    df_excel.to_excel(output_path, index=False, engine='openpyxl')
    logger.info(f"Written Excel output: {output_path}")


def get_canonical_columns() -> List[str]:
    """Return the canonical schema column names."""
    return [
        'city', 'business_name', 'category', 'address', 'phone_raw',
        'website_raw', 'google_maps_url', 'rating', 'reviews_count',
        'lat', 'lon'
    ]
