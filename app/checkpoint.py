"""
Progress checkpointing for the enrichment pipeline.

Saves intermediate results to disk so the pipeline can resume after crashes.
Keeps only the latest 3 checkpoints and automatically cleans up older ones.
"""

import glob
import logging
import os
import pickle
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

VALID_STEPS: List[str] = [
    "cleaned",
    "deduped",
    "filtered",
    "enriched",
    "decision_maker",
    "company_intel",
    "scored",
]

MAX_CHECKPOINTS: int = 3


class PipelineCheckpoint:
    """Manages pipeline checkpoints for crash recovery.

    Saves DataFrame state along with metadata at each pipeline step,
    allowing the pipeline to resume from the last successful checkpoint.

    Args:
        checkpoint_dir: Directory to store checkpoint files.
    """

    def __init__(self, checkpoint_dir: str = "./cache/checkpoints") -> None:
        self.checkpoint_dir: str = checkpoint_dir
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        logger.debug("Checkpoint directory ready: %s", self.checkpoint_dir)

    def save(self, df: pd.DataFrame, step: str, stats: Dict[str, Any]) -> None:
        """Save current pipeline state to a checkpoint file.

        Args:
            df: Current DataFrame to persist.
            step: Pipeline step name (e.g. 'cleaned', 'enriched').
            stats: Dictionary of statistics collected so far.

        Raises:
            ValueError: If step is not a recognised pipeline step.
        """
        if step not in VALID_STEPS:
            raise ValueError(
                f"Invalid step '{step}'. Must be one of: {VALID_STEPS}"
            )

        timestamp: str = str(int(time.time()))
        filename: str = f"checkpoint_{step}_{timestamp}.pkl"
        filepath: str = os.path.join(self.checkpoint_dir, filename)

        payload: Dict[str, Any] = {
            "dataframe": df,
            "step": step,
            "stats": stats,
            "timestamp": timestamp,
            "record_count": len(df),
        }

        try:
            with open(filepath, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info(
                "Checkpoint saved: step=%s, records=%d, file=%s",
                step,
                len(df),
                filename,
            )
        except Exception as e:
            logger.error("Failed to save checkpoint: %s", e)
            raise

        self._cleanup_old_checkpoints()

    def load(self) -> Optional[Tuple[pd.DataFrame, str, Dict[str, Any]]]:
        """Load the most recent checkpoint.

        Returns:
            Tuple of (DataFrame, step_name, stats_dict), or None if no
            checkpoint exists.
        """
        latest: Optional[str] = self.get_latest()
        if latest is None:
            logger.info("No checkpoint found to load")
            return None

        try:
            with open(latest, "rb") as f:
                payload: Dict[str, Any] = pickle.load(f)

            df: pd.DataFrame = payload["dataframe"]
            step: str = payload["step"]
            stats: Dict[str, Any] = payload["stats"]
            record_count: int = payload.get("record_count", len(df))

            logger.info(
                "Checkpoint loaded: step=%s, records=%d, file=%s",
                step,
                record_count,
                os.path.basename(latest),
            )
            return df, step, stats
        except Exception as e:
            logger.error("Failed to load checkpoint '%s': %s", latest, e)
            return None

    def get_latest(self) -> Optional[str]:
        """Get the file path of the most recent checkpoint.

        Returns:
            Absolute path to the latest checkpoint file, or None if no
            checkpoints exist.
        """
        pattern: str = os.path.join(self.checkpoint_dir, "checkpoint_*.pkl")
        files: List[str] = sorted(glob.glob(pattern), key=os.path.getmtime)
        if not files:
            return None
        return files[-1]

    def clear(self) -> None:
        """Remove all checkpoint files from the checkpoint directory."""
        pattern: str = os.path.join(self.checkpoint_dir, "checkpoint_*.pkl")
        files: List[str] = glob.glob(pattern)
        for filepath in files:
            try:
                os.remove(filepath)
                logger.debug("Removed checkpoint: %s", os.path.basename(filepath))
            except OSError as e:
                logger.warning("Could not remove '%s': %s", filepath, e)
        if files:
            logger.info("Cleared %d checkpoint file(s)", len(files))

    def should_save(self, records_processed: int, interval: int = 50) -> bool:
        """Check whether a checkpoint should be saved based on record count.

        Args:
            records_processed: Number of records processed so far.
            interval: Save a checkpoint every *interval* records.

        Returns:
            True if records_processed is a positive multiple of interval.
        """
        if records_processed <= 0:
            return False
        return records_processed % interval == 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup_old_checkpoints(self) -> None:
        """Keep only the latest MAX_CHECKPOINTS files, delete the rest."""
        pattern: str = os.path.join(self.checkpoint_dir, "checkpoint_*.pkl")
        files: List[str] = sorted(glob.glob(pattern), key=os.path.getmtime)

        if len(files) <= MAX_CHECKPOINTS:
            return

        to_remove: List[str] = files[: len(files) - MAX_CHECKPOINTS]
        for filepath in to_remove:
            try:
                os.remove(filepath)
                logger.debug(
                    "Cleaned up old checkpoint: %s", os.path.basename(filepath)
                )
            except OSError as e:
                logger.warning("Could not remove old checkpoint '%s': %s", filepath, e)

        logger.debug(
            "Checkpoint cleanup: removed %d, kept %d",
            len(to_remove),
            MAX_CHECKPOINTS,
        )
