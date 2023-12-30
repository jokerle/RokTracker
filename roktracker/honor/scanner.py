import math
import time

from cv2.typing import MatLike
from dummy_root import get_app_root
from roktracker.alliance.additional_data import AdditionalData
from roktracker.alliance.excel_handler import ExcelHandler
from roktracker.alliance.governor_data import GovernorData
from roktracker.alliance.governor_image_group import GovImageGroup
from roktracker.honor.ui_settings import HonorUI
from roktracker.utils.adb import *
from roktracker.utils.general import *
from roktracker.utils.ocr import *
from tesserocr import PyTessBaseAPI, PSM, OEM  # type: ignore
from typing import Callable, List


def default_batch_callback(govs: List[GovernorData], extra: AdditionalData) -> None:
    pass


def default_output_handler(msg: str) -> None:
    console.log(msg)
    pass


class HonorScanner:
    def __init__(self, port):
        self.run_id = generate_random_id(8)
        self.start_date = datetime.date.today()
        self.stop_scan = False
        self.scan_times = []

        self.reached_bottom = False
        self.govs_per_screen = 5
        self.screens_needed = 0

        # TODO: Load paths from config
        self.root_dir = get_app_root()
        self.tesseract_path = Path(self.root_dir / "deps" / "tessdata")
        self.img_path = Path(self.root_dir / "temp_images")
        self.img_path.mkdir(parents=True, exist_ok=True)
        self.scan_path = Path(self.root_dir / "scans_honor")
        self.scan_path.mkdir(parents=True, exist_ok=True)

        self.batch_callback = default_batch_callback
        self.output_handler = default_output_handler

        self.adb_client = AdvancedAdbClient(
            str(self.root_dir / "deps" / "platform-tools" / "adb.exe"), port
        )

    def set_batch_callback(
        self, cb: Callable[[List[GovernorData], AdditionalData], None]
    ) -> None:
        self.batch_callback = cb

    def set_output_handler(self, cb: Callable[[str], None]):
        self.output_handler = cb

    def get_remaining_time(self, remaining_govs: int) -> float:
        return (sum(self.scan_times, start=0) / len(self.scan_times)) * remaining_govs

    def process_honor_screen(self, image: MatLike, position: int) -> GovImageGroup:
        # fmt: off
        gov_name_im = cropToRegion(image, HonorUI.name[position])
        gov_name_im_bw = preprocessImage(
            gov_name_im, 3, HonorUI.misc.threshold,
            12, HonorUI.misc.invert,
        )

        gov_name_im_bw_small = preprocessImage(
            gov_name_im, 1, HonorUI.misc.threshold,
            4, HonorUI.misc.invert,
        )

        gov_score_im = cropToRegion(image, HonorUI.score[position])
        gov_score_im_bw = preprocessImage(
            gov_score_im, 3, HonorUI.misc.threshold,
            12, HonorUI.misc.invert,
        )
        # fmt: on

        return GovImageGroup(gov_name_im_bw, gov_name_im_bw_small, gov_score_im_bw)

    def scan_screen(self, screen_number: int) -> List[GovernorData]:
        # Take screenshot to process
        self.adb_client.secure_adb_screencap().save(self.img_path / "currentState.png")
        image = cv2.imread(str(self.img_path / "currentState.png"))

        # Actual scanning
        govs = []
        with PyTessBaseAPI(
            path=str(self.tesseract_path), psm=PSM.SINGLE_LINE, oem=OEM.LSTM_ONLY
        ) as api:
            for gov_number in range(0, self.govs_per_screen):
                gov = self.process_honor_screen(image, gov_number)
                api.SetPageSegMode(PSM.SINGLE_LINE)
                gov_name = ocr_text(api, gov.name_img)

                api.SetPageSegMode(PSM.SINGLE_WORD)
                gov_score = ocr_number(api, gov.score_img)

                # fmt: off
                gov_img_path = str(self.img_path / f"gov_name_{(6 * screen_number) + 1}.png")
                cv2.imwrite(gov_img_path, gov.name_img_small)
                # fmt: on

                govs.append(GovernorData(gov_img_path, gov_name, gov_score))

        return govs

    def start_scan(self, kingdom: str, amount: int):
        self.screens_needed = int(math.ceil(amount / self.govs_per_screen))

        filename = f"Honor{amount}-{self.start_date}-{kingdom}-[{self.run_id}].xlsx"

        ######Excel Formatting
        excel = ExcelHandler(
            str(self.scan_path / filename),
            self.start_date,
        )

        for i in range(0, self.screens_needed):
            if self.stop_scan:
                self.output_handler("Scan Terminated! Saving the current progress...")
                break

            start_time = time.time()
            governors = self.scan_screen(i)
            end_time = time.time()

            self.scan_times.append(end_time - start_time)

            additional_data = AdditionalData(
                i,
                amount,
                self.govs_per_screen,
                self.get_remaining_time(self.screens_needed - i),
            )

            self.batch_callback(governors, additional_data)

            self.reached_bottom = (
                excel.add_results_to_sheet(governors, i) or self.reached_bottom
            )
            excel.save()

            if self.reached_bottom:
                break
            else:
                self.adb_client.adb_send_events("Touch", HonorUI.misc.script)
                time.sleep(1 + random.random())

        excel.save()
        self.adb_client.kill_adb()  # make sure to clean up adb server

        for p in self.img_path.glob("gov_name*.png"):
            p.unlink()

        return

    def end_scan(self) -> None:
        self.stop_scan = True
