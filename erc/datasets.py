import os
import logging
from pathlib import Path
from collections import OrderedDict

from scipy.io import wavfile
import numpy as np
import pandas as pd
from tqdm.auto import tqdm
import torch
from torch.utils.data import Dataset

from .utils import check_exists


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class KEMDy19Dataset(Dataset):
    dataset = "KEMDy19"
    wav_txt_path_fmt = "./data/KEMDy19/wav/Session{0}/Sess{0}_{1}"
    eda_path_fmt = "./data/KEMDy19/EDA/Session{0}/Original/Sess{0}{1}.csv"

    # Annotation (ECG / EDA / Emotion / Valence & Arousal)
    male_annot_expr = "./annotation/Session*_M_*"
    female_annot_expr = "./annotation/Session*_F_*"
    TOTAL_DF_PATH = "./data/kemdy19.csv"

    def __init__(
        self,
        base_path: str,
        generate_csv: bool = False,
        return_full_bio: bool = False,
    ):
        """
        Args:
            cfg: yaml file
            generate_csv:
                Flag to generate a new label.csv, default=False
            return_full_bio:
                Flag to call and return full ECG / EDA / TEMP data
                Since csv in annotation directory contains start/end value of above signals,
                this is not necessary. default=False
        """
        logger.info("Instantiate KEMDy19 Dataset")
        self.base_path: Path = Path(base_path)
        self.return_full_bio = return_full_bio

        self.total_df: pd.DataFrame = self.processed_db(generate_csv=generate_csv)

    def __len__(self):
        return len(self.total_df)

    def __getitem__(self, idx):
        data = {}
        row = self.total_df.iloc[idx]
        segment_id = row["segmend_id"]
        session, script_type, speaker = segment_id.split("_")
        # prefix: 'KEMDy19/wav/Session01/Sess01_impro01'
        wav_prefix = Path(self.wav_txt_path_fmt.format(session[-2:], script_type))

        # Wave File
        wav_path = wav_prefix / f"{segment_id}.wav"
        sampling_rate, wav = self.get_wav(wav_path=wav_path)
        data["sampling_rate"] = sampling_rate
        data["wav"] = wav

        # Txt File
        txt_path = wav_prefix / f"{segment_id}.txt"
        data["txt"] = self.get_txt(txt_path=txt_path)
        
        # Bio Signals
        # Currently returns average
        if self.return_full_bio:
            # Contains full eda
            # TODO WIP
            eda_path: str = self.eda_path_fmt.format(session[-2:], speaker[0])
            eda: pd.DataFrame = eda_preprocess(file_path=eda_path)
        else:
            for bio in ["ecg", "e4-eda", "e4-temp"]:
                s, e = map(float, row[[f"{bio}_start", f"{bio}_end"]])
                data[bio] = torch.tensor((s + e) / 2, dtype=torch.float)
                
        # Emotion
        data["emotion"] = self.str2num(row["emotion"])

        # Valence & Arousal
        valence, arousal = map(float, row[["valence", "arousal"]])
        data["valence"] = torch.tensor(valence, dtype=torch.float)
        data["arousal"] = torch.tensor(arousal, dtype=torch.float)

        return data

    def get_wav(self, wav_path: Path | str) -> torch.Tensor | np.ndarray:
        """ Get output feature vector from pre-trained wav2vec model
        XXX: Embedding outside dataset, to fine-tune pre-trained model? See Issue
        """
        wav_path = check_exists(wav_path)
        sampling_rate, data = wavfile.read(wav_path)
        return sampling_rate, data

    def get_txt(self, txt_path: Path | str) -> torch.Tensor:
        """ Get output feature vector from pre-trained txt model
        TODO:
            1. How to process special cases: /o /l 
                -> They _maybe_ processed by pre-trained toeknizers
            2. Which model to use
        """
        txt_path = check_exists(txt_path)
        with open(txt_path, mode="r") as f:
            txt = f.readlines()
        return txt

    def processed_db(self, generate_csv: bool = False) -> pd.DataFrame:
        """ Reads in .csv file if exists.
        If pre-processed .csv file does NOT exists, read from data path. """
        if not os.path.exists(self.TOTAL_DF_PATH) or generate_csv:
            logger.info(f"{self.TOTAL_DF_PATH} does not exists. Process from raw data")
            total_df = self.make_total_df()
        else:
            try:
                total_df = pd.read_csv(self.TOTAL_DF_PATH)
            except pd.errors.EmptyDataError as e:
                logger.error(f"{self.TOTAL_DF_PATH} seems to be empty")
                logger.exception(e)
                total_df = None
        return total_df

    def make_total_df(self) -> pd.DataFrame:
        # .csv 상태가 나빠서 위치로 기억하는 것이 나음
        selected_columns = OrderedDict({
            "Numb": "Numb",
            "Wav": "wav_start",
            "Unnamed: 2": "wav_end",
            "ECG": "ecg_start",
            "Unnamed: 4": "ecg_end",
            "E4-EDA": "e4-eda_start",
            "Unnamed: 6": "e4-eda_end",
            "E4-TEMP": "e4-temp_start",
            "Unnamed: 8": "e4-temp_end",
            "Segment ID": "segmend_id",
            "Total Evaluation": "emotion",
            "Unnamed: 11": "valence",
            "Unnamed: 12": "arousal",
        })
        male_annots = sorted(self.base_path.rglob(self.male_annot_expr))
        female_annots = sorted(self.base_path.rglob(self.female_annot_expr))
        assert (len(male_annots) > 0) & (len(female_annots) > 0),\
            f"Annotations does not exists. Check {self.base_path / self.male_annot_expr}"

        total_df = pd.DataFrame()
        pbar = tqdm(
            iterable=zip(male_annots, female_annots),
            desc="Processing ECG / EDA / Label",
            total=len(male_annots)
        )
        for m_ann, f_ann in pbar:
            m_df = pd.read_csv(m_ann).dropna()[list(selected_columns.keys())]
            f_df = pd.read_csv(f_ann).dropna()[list(selected_columns.keys())]

            # Sess01_impro03, Sess01_impro04의 TEMP와 E4-EDA값이 결측
            m_df = m_df[
                ~(m_df["Segment ID"].str.contains("Sess01_impro03"))
                & ~(m_df["Segment ID"].str.contains("Sess01_impro04"))
            ]
            f_df = f_df[
                ~(f_df["Segment ID"].str.contains("Sess01_impro03"))
                & ~(f_df["Segment ID"].str.contains("Sess01_impro04"))
            ]

            # 각 발화의 성별에 대한 감정만 추출
            m_df = m_df[m_df["Segment ID"].str.contains("M")]
            f_df = f_df[f_df["Segment ID"].str.contains("F")]

            # 다시 합쳐서 정렬
            tmp_df = pd.concat([m_df, f_df], axis=0).sort_values("Numb")
            total_df = pd.concat([total_df, tmp_df], axis=0)
        
        logger.info(f"New dataframe saved as {self.TOTAL_DF_PATH}")
        total_df.columns = list(selected_columns.values())
        total_df.to_csv(self.TOTAL_DF_PATH, index=False)
        return total_df

    @staticmethod
    def str2num(key: str) -> torch.Tensor:
        emotion2idx = {
            "surprise": 1,
            "fear": 2,
            "angry": 3,
            "neutral": 4,
            "happy": 5,
            "sad": 6,
            "disgust": 7,
        }
        emotion = emotion2idx.get(key, 0)
        return torch.tensor(emotion, dtype=torch.long)


def eda_preprocess(file_path: str) -> pd.DataFrame:
    """ on_bad_line이 있어서 (column=4 or  3으로 일정하지 않아) 4줄로 통일 하는 함수 """
    columns = ["EDA_value", "a", "b", "Segment ID"]

    with open(file_path, "r") as f:
        lines = f.readlines()

    new_lines = []
    for line in tqdm(lines):
        line = line.rstrip()
        if len(line.split(",")) <= 3:
            line += ",None"  # 4줄로 만들어주기 .
        new_lines.append(line.split(","))

    return pd.DataFrame(new_lines, columns=columns).replace("None", np.nan).dropna()


if __name__=="__main__":
    dataset = KEMDy19Dataset(base_path="~/codespace/etri-erc/data/KEMDy19",
                             generate_csv=True,
                             return_full_bio=False,
                             wav2vec_pretrain="facebook/wav2vec2-base-960h")
    print(dataset[0])