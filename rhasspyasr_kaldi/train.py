"""Methods for generating ASR artifacts."""
import logging
import os
import shutil
import subprocess
import tempfile
import typing
from pathlib import Path

import networkx as nx
import rhasspynlu
from rhasspynlu.g2p import PronunciationsType

_DIR = Path(__file__).parent
_LOGGER = logging.getLogger(__name__)

# -----------------------------------------------------------------------------


def get_kaldi_dir() -> Path:
    """Get directory to Kaldi installation."""
    # Check environment variable
    if "KALDI_DIR" in os.environ:
        return Path(os.environ["KALDI_DIR"])

    return _DIR / "kaldi"


# -----------------------------------------------------------------------------


def train(
    graph: nx.DiGraph,
    pronunciations: PronunciationsType,
    model_dir: typing.Union[str, Path],
    graph_dir: typing.Union[str, Path],
    dictionary: typing.Optional[typing.Union[str, Path]] = None,
    language_model: typing.Optional[typing.Union[str, Path]] = None,
    dictionary_word_transform: typing.Optional[typing.Callable[[str], str]] = None,
    g2p_model: typing.Optional[typing.Union[str, Path]] = None,
    g2p_word_transform: typing.Optional[typing.Callable[[str], str]] = None,
    missing_words_path: typing.Optional[Path] = None,
    vocab_path: typing.Optional[typing.Union[str, Path]] = None,
    language_model_fst: typing.Optional[typing.Union[str, Path]] = None,
    base_language_model_fst: typing.Optional[typing.Union[str, Path]] = None,
    base_language_model_weight: typing.Optional[float] = None,
    mixed_language_model_fst: typing.Optional[typing.Union[str, Path]] = None,
    balance_counts: bool = True,
    kaldi_dir: typing.Optional[Path] = None,
):
    """Re-generates HCLG.fst from intent graph"""
    g2p_word_transform = g2p_word_transform or (lambda s: s)

    # Determine directory with Kaldi binaries
    if kaldi_dir is None:
        kaldi_dir = get_kaldi_dir()

    assert kaldi_dir is not None
    _LOGGER.debug("Using kaldi at %s", str(kaldi_dir))

    vocabulary: typing.Set[str] = set()
    if vocab_path:
        vocab_file = open(vocab_path, "w+")
    else:
        vocab_file = typing.cast(
            typing.TextIO, tempfile.NamedTemporaryFile(suffix=".txt", mode="w+")
        )
        vocab_path = vocab_file.name

    # Language model mixing
    is_mixing = False
    base_fst_weight = None
    if (
        (base_language_model_fst is not None)
        and (base_language_model_weight is not None)
        and (base_language_model_weight > 0)
    ):
        is_mixing = True
        base_fst_weight = (base_language_model_fst, base_language_model_weight)

    # Begin training
    with tempfile.NamedTemporaryFile(mode="w+") as lm_file:
        with vocab_file:
            # Create language model
            _LOGGER.debug("Converting to ARPA language model")
            rhasspynlu.arpa_lm.graph_to_arpa(
                graph,
                lm_file.name,
                vocab_path=vocab_path,
                model_path=language_model_fst,
                base_fst_weight=base_fst_weight,
                merge_path=mixed_language_model_fst,
            )

            # Load vocabulary
            vocab_file.seek(0)
            vocabulary.update(line.strip() for line in vocab_file)

            if is_mixing:
                # Add all known words
                vocabulary.update(pronunciations.keys())

        assert vocabulary, "No words in vocabulary"

        # Write dictionary to temporary file
        with tempfile.NamedTemporaryFile(mode="w+") as dictionary_file:
            _LOGGER.debug("Writing pronunciation dictionary")
            rhasspynlu.g2p.write_pronunciations(
                vocabulary,
                pronunciations,
                dictionary_file.name,
                g2p_model=g2p_model,
                g2p_word_transform=g2p_word_transform,
                missing_words_path=missing_words_path,
            )

            # -----------------------------------------------------------------

            dictionary_file.seek(0)
            if dictionary:
                # Copy dictionary over real file
                shutil.copy(dictionary_file.name, dictionary)
                _LOGGER.debug("Wrote dictionary to %s", str(dictionary))
            else:
                dictionary = Path(dictionary_file.name)
                dictionary_file.seek(0)

            lm_file.seek(0)
            if language_model:
                # Copy language model over real file
                shutil.copy(lm_file.name, language_model)
                _LOGGER.debug("Wrote language model to %s", str(language_model))
            else:
                language_model = Path(lm_file.name)
                lm_file.seek(0)

            # Generate HCLG.fst
            train_kaldi(
                model_dir, graph_dir, dictionary, language_model, kaldi_dir=kaldi_dir
            )


# -----------------------------------------------------------------------------


def train_kaldi(
    model_dir: typing.Union[str, Path],
    graph_dir: typing.Union[str, Path],
    dictionary: typing.Union[str, Path],
    language_model: typing.Union[str, Path],
    kaldi_dir: typing.Union[str, Path],
):
    """Generates HCLG.fst from dictionary and language model."""

    # Convert to paths
    model_dir = Path(model_dir)
    graph_dir = Path(graph_dir)
    kaldi_dir = Path(kaldi_dir)

    # -------------------------------------------------------------------------
    # Kaldi Training
    # ---------------------------------------------------------
    # 1. prepare_lang.sh
    # 2. format_lm.sh
    # 3. mkgraph.sh
    # 4. prepare_online_decoding.sh
    # ---------------------------------------------------------

    # Extend PATH
    egs_utils_dir = kaldi_dir / "egs" / "wsj" / "s5" / "utils"
    extended_env = os.environ.copy()
    extended_env["PATH"] = (
        str(kaldi_dir) + ":" + str(egs_utils_dir) + ":" + extended_env["PATH"]
    )

    # Create empty path.sh
    path_sh = model_dir / "path.sh"
    if not path_sh.is_file():
        path_sh.write_text("")

    # Delete existing data/graph
    data_dir = model_dir / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)

    if graph_dir.exists():
        shutil.rmtree(graph_dir)

    data_local_dir = model_dir / "data" / "local"

    _LOGGER.debug("Generating lexicon")
    dict_local_dir = data_local_dir / "dict"
    dict_local_dir.mkdir(parents=True, exist_ok=True)

    # Copy phones
    phones_dir = model_dir / "phones"
    for phone_file in phones_dir.glob("*.txt"):
        shutil.copy(phone_file, dict_local_dir / phone_file.name)

    # Copy dictionary
    shutil.copy(dictionary, dict_local_dir / "lexicon.txt")

    # Create utils link
    model_utils_link = model_dir / "utils"

    try:
        # Can't use missing_ok in 3.6
        model_utils_link.unlink()
    except Exception:
        pass

    model_utils_link.symlink_to(egs_utils_dir, target_is_directory=True)

    # 1. prepare_lang.sh
    lang_dir = data_dir / "lang"
    lang_local_dir = data_local_dir / "lang"
    prepare_lang = [
        "bash",
        str(egs_utils_dir / "prepare_lang.sh"),
        str(dict_local_dir),
        "",
        str(lang_local_dir),
        str(lang_dir),
    ]

    _LOGGER.debug(prepare_lang)
    subprocess.check_call(prepare_lang, cwd=model_dir, env=extended_env)

    # 2. format_lm.sh
    lm_arpa = lang_local_dir / "lm.arpa"
    lm_arpa.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(language_model, lm_arpa)

    gzip_lm = ["gzip", str(lm_arpa)]
    _LOGGER.debug(gzip_lm)
    subprocess.check_call(gzip_lm, cwd=lm_arpa.parent, env=extended_env)

    format_lm = [
        "bash",
        str(egs_utils_dir / "format_lm.sh"),
        str(lang_dir),
        str(lm_arpa.with_suffix(".arpa.gz")),
        str(dict_local_dir / "lexicon.txt"),
        str(lang_dir),
    ]

    _LOGGER.debug(format_lm)
    subprocess.check_call(format_lm, cwd=model_dir, env=extended_env)

    # 3. mkgraph.sh
    mkgraph = [
        "bash",
        str(egs_utils_dir / "mkgraph.sh"),
        str(lang_dir),
        str(model_dir / "model"),
        str(graph_dir),
    ]
    _LOGGER.debug(mkgraph)
    subprocess.check_call(mkgraph, cwd=model_dir, env=extended_env)

    # 4. prepare_online_decoding.sh
    train_prepare_online_decoding(model_dir, lang_dir, kaldi_dir)


def train_prepare_online_decoding(
    model_dir: typing.Union[str, Path],
    lang_dir: typing.Union[str, Path],
    kaldi_dir: typing.Union[str, Path],
):
    """Prepare model for online decoding."""
    model_dir = Path(model_dir)
    kaldi_dir = Path(kaldi_dir)

    # prepare_online_decoding.sh (nnet3 only)
    extractor_dir = model_dir / "extractor"
    if extractor_dir.is_dir():
        # Extend PATH
        egs_utils_dir = kaldi_dir / "egs" / "wsj" / "s5" / "utils"
        extended_env = os.environ.copy()
        extended_env["PATH"] = (
            str(kaldi_dir) + ":" + str(egs_utils_dir) + ":" + extended_env["PATH"]
        )

        # Create empty path.sh
        path_sh = model_dir / "path.sh"
        if not path_sh.is_file():
            path_sh.write_text("")

        # Create utils link
        model_utils_link = model_dir / "utils"

        try:
            # Can't use missing_ok in 3.6
            model_utils_link.unlink()
        except Exception:
            pass

        model_utils_link.symlink_to(egs_utils_dir, target_is_directory=True)

        # Generate online.conf
        mfcc_conf = model_dir / "conf" / "mfcc_hires.conf"
        egs_steps_dir = kaldi_dir / "egs" / "wsj" / "s5" / "steps"
        prepare_online_decoding = [
            "bash",
            str(egs_steps_dir / "online" / "nnet3" / "prepare_online_decoding.sh"),
            "--mfcc-config",
            str(mfcc_conf),
            str(lang_dir),
            str(extractor_dir),
            str(model_dir / "model"),
            str(model_dir / "online"),
        ]

        _LOGGER.debug(prepare_online_decoding)
        subprocess.run(
            prepare_online_decoding,
            cwd=model_dir,
            env=extended_env,
            stderr=subprocess.STDOUT,
            check=True,
        )


# -----------------------------------------------------------------------------
