import os
import platform
import sys
import time
from base64 import b64encode
from datetime import datetime
from random import choice, randint

from typing import Dict, List

import cloudscraper

from selenium.webdriver import ActionChains
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from tools.clog import CLogger
from tools.utils import retry_on_failure, desktop_notification

try:
    import beepy

    ENABLE_BEEPY = True
except ImportError:
    ENABLE_BEEPY = False


class ImpfterminService():
    def __init__(self, code: str, plz_impfzentren: list, kontakt: dict, PATH: str):
        self.code = str(code).upper()
        self.splitted_code = self.code.split("-")

        self.PATH = PATH

        # PLZ's zu String umwandeln
        self.plz_impfzentren = sorted([str(plz) for plz in plz_impfzentren])
        self.plz_termin = None

        self.kontakt = kontakt
        self.authorization = b64encode(bytes(f":{code}", encoding='utf-8')).decode("utf-8")

        # Logging einstellen
        self.log = CLogger("impfterminservice")
        self.log.set_prefix(f"*{self.code[-4:]} | {', '.join(self.plz_impfzentren)}")

        # Session erstellen
        self.s = cloudscraper.create_scraper()
        self.s.headers.update({
            'Authorization': f'Basic {self.authorization}',
            'User-Agent': 'Mozilla/5.0',
        })

        # Ausgewähltes Impfzentrum prüfen
        self.verfuegbare_impfzentren = {}
        self.impfzentrum = {}
        self.domain = None
        if not self.impfzentren_laden():
            raise ValueError("Impfzentren laden fehlgeschlagen")

        # Verfügbare Impfstoffe laden
        self.verfuegbare_qualifikationen: List[Dict] = []
        while not self.impfstoffe_laden():
            self.log.warn("Erneuter Versuch in 60 Sekunden")
            time.sleep(60)

        # OS
        self.operating_system = platform.system().lower()

        # Sonstige
        self.terminpaar = None
        self.qualifikationen = []
        self.app_name = str(self)

    def __str__(self) -> str:
        return "ImpfterminService"

    @retry_on_failure()
    def impfzentren_laden(self):
        """
        Laden aller Impfzentren zum Abgleich der eingegebenen PLZ.

        :return: bool
        """

        url = "https://www.impfterminservice.de/assets/static/impfzentren.json"

        res = self.s.get(url, timeout=15)
        if res.ok:
            # Antwort-JSON umformatieren für einfachere Handhabung
            formatierte_impfzentren = {}
            for bundesland, impfzentren in res.json().items():
                for impfzentrum in impfzentren:
                    formatierte_impfzentren[impfzentrum["PLZ"]] = impfzentrum

            self.verfuegbare_impfzentren = formatierte_impfzentren
            self.log.info(f"{len(self.verfuegbare_impfzentren)} Impfzentren verfügbar")

            # Prüfen, ob Impfzentren zur eingetragenen PLZ existieren
            plz_geprueft = []
            for plz in self.plz_impfzentren:
                self.impfzentrum = self.verfuegbare_impfzentren.get(plz)
                if self.impfzentrum:
                    self.domain = self.impfzentrum.get("URL")
                    self.log.info("'{}' in {} {} ausgewählt".format(
                        self.impfzentrum.get("Zentrumsname").strip(),
                        self.impfzentrum.get("PLZ"),
                        self.impfzentrum.get("Ort")))
                    plz_geprueft.append(plz)

            if plz_geprueft:
                self.plz_impfzentren = plz_geprueft
                return True
            else:
                self.log.error("Kein Impfzentrum zu eingetragenen PLZ's verfügbar.")
                return False
        else:
            self.log.error("Impfzentren können nicht geladen werden")
        return False

    @retry_on_failure(1)
    def impfstoffe_laden(self):
        """
        Laden der verfügbaren Impstoff-Qualifikationen.
        In der Regel gibt es 3 Qualifikationen, die je nach Altersgruppe verteilt werden.

        :return:
        """
        path = "assets/static/its/vaccination-list.json"

        res = self.s.get(self.domain + path, timeout=15)
        if res.ok:
            res_json = res.json()

            for qualifikation in res_json:
                qualifikation["impfstoffe"] = qualifikation.get("tssname",
                                                                "N/A").replace(" ", "").split(",")
                self.verfuegbare_qualifikationen.append(qualifikation)

            # Ausgabe der verfügbaren Impfstoffe:
            for qualifikation in self.verfuegbare_qualifikationen:
                q_id = qualifikation["qualification"]
                alter = qualifikation.get("age", "N/A")
                intervall = qualifikation.get("interval", " ?")
                impfstoffe = str(qualifikation["impfstoffe"])
                self.log.info(
                    f"[{q_id}] Altersgruppe: {alter} (Intervall: {intervall} Tage) --> {impfstoffe}")
            print("")
            return True

        self.log.error("Keine Impfstoffe im ausgewählten Impfzentrum verfügbar")
        return False

    def get_chromedriver_path(self):
        """
        :return: String mit Pfad zur chromedriver-Programmdatei
        """
        chromedriver_from_env = os.getenv("VACCIPY_CHROMEDRIVER")
        if chromedriver_from_env:
            return chromedriver_from_env

        # Chromedriver anhand des OS auswählen
        if 'linux' in self.operating_system:
            if "64" in platform.architecture() or sys.maxsize > 2 ** 32:
                return os.path.join(self.PATH, "tools/chromedriver/chromedriver-linux-64")
            else:
                return os.path.join(self.PATH, "tools/chromedriver/chromedriver-linux-32")
        elif 'windows' in self.operating_system:
            return os.path.join(self.PATH, "tools/chromedriver/chromedriver-windows.exe")
        elif 'darwin' in self.operating_system:
            if "arm" in platform.processor().lower():
                return os.path.join(self.PATH, "tools/chromedriver/chromedriver-mac-m1")
            else:
                return os.path.join(self.PATH, "tools/chromedriver/chromedriver-mac-intel")
        else:
            raise ValueError(f"Nicht unterstütztes Betriebssystem {self.operating_system}")

    def get_chromedriver(self, headless):
        chrome_options = Options()

        # deaktiviere Selenium Logging
        chrome_options.add_argument('disable-infobars')
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])

        # Chrome head is only required for the backup booking process.
        # User-Agent is required for headless, because otherwise the server lets us hang.
        chrome_options.add_argument("user-agent=Mozilla/5.0")

        chrome_options.headless = headless

        return Chrome(self.get_chromedriver_path(), options=chrome_options)

    def driver_enter_code(self, driver, plz_impfzentrum):
        """
        TODO xpath code auslagern
        """
        url = f"{self.domain}impftermine/service?plz={plz_impfzentrum}"

        driver.get(url)

        # Queue Bypass
        queue_cookie = driver.get_cookie("akavpwr_User_allowed")
        if queue_cookie:
            self.log.info("Im Warteraum, Seite neuladen")
            queue_cookie["name"] = "akavpau_User_allowed"
            driver.add_cookie(queue_cookie)

            # Seite neu laden
            driver.get(url)
            driver.refresh()

        # Klick auf "Auswahl bestätigen" im Cookies-Banner
        # Warteraum-Support: Timeout auf 1 Stunde
        button_xpath = ".//html/body/app-root/div/div/div/div[2]/div[2]/div/div[1]/a"
        button = WebDriverWait(driver, 60 * 60).until(
            EC.element_to_be_clickable((By.XPATH, button_xpath)))
        action = ActionChains(driver)
        action.move_to_element(button).click().perform()

        # Klick auf "Vermittlungscode bereits vorhanden"
        button_xpath = "/html/body/app-root/div/app-page-its-login/div/div/div[2]/app-its-login-user/" \
                       "div/div/app-corona-vaccination/div[2]/div/div/label[1]/span"
        button = WebDriverWait(driver, 1).until(
            EC.element_to_be_clickable((By.XPATH, button_xpath)))
        action = ActionChains(driver)
        action.move_to_element(button).click().perform()

        # Auswahl des ersten Code-Input-Feldes
        input_xpath = "/html/body/app-root/div/app-page-its-login/div/div/div[2]/app-its-login-user/" \
                      "div/div/app-corona-vaccination/div[3]/div/div/div/div[1]/app-corona-vaccination-yes/" \
                      "form[1]/div[1]/label/app-ets-input-code/div/div[1]/label/input"
        input_field = WebDriverWait(driver, 1).until(
            EC.element_to_be_clickable((By.XPATH, input_xpath)))
        action = ActionChains(driver)
        action.move_to_element(input_field).click().perform()

        # Code eintragen
        input_field.send_keys(self.code)
        time.sleep(.1)

        # Klick auf "Termin suchen"
        button_xpath = "/html/body/app-root/div/app-page-its-login/div/div/div[2]/app-its-login-user/" \
                       "div/div/app-corona-vaccination/div[3]/div/div/div/div[1]/app-corona-vaccination-yes/" \
                       "form[1]/div[2]/button"
        button = WebDriverWait(driver, 1).until(
            EC.element_to_be_clickable((By.XPATH, button_xpath)))
        action = ActionChains(driver)
        action.move_to_element(button).click().perform()

        # Maus-Bewegung hinzufügen (nicht sichtbar)
        for i in range(3):
            try:
                action.move_by_offset(randint(1, 100), randint(1, 100)).perform()
                time.sleep(randint(1, 3))
            except:
                pass

    def driver_renew_cookies(self, driver, plz_impfzentrum):
        self.driver_enter_code(driver, plz_impfzentrum)

        # prüfen, ob Cookies gesetzt wurden und in Session übernehmen
        try:
            cookie = driver.get_cookie("bm_sz")
            if cookie:
                self.s.cookies.clear()
                self.s.cookies.update({c['name']: c['value'] for c in driver.get_cookies()})
                self.log.info("Browser-Cookie generiert: *{}".format(cookie.get("value")[-6:]))
                return True
            else:
                self.log.error("Cookies können nicht erstellt werden!")
                return False
        except:
            return False


    def driver_renew_cookies_code(self, driver, plz_impfzentrum, manual=False):
        self.driver_enter_code(driver, plz_impfzentrum)
        if manual:
            self.log.warn(
                "Du hast jetzt 30 Sekunden Zeit möglichst viele Elemente im Chrome Fenster anzuklicken. Das Fenster schließt sich automatisch.")
            time.sleep(30)
        # prüfen, ob Cookies gesetzt wurden und in Session übernehmen
        try:
            cookie = driver.get_cookie("bm_sz").get("value")
            akavpau = driver.get_cookie("akavpau_User_allowed").get("value")
            if cookie:
                self.s.cookies.clear()
                self.s.cookies.update({"bm_sz": cookie,"akavpau_User_allowed": akavpau })
                self.log.info("Browser-Cookie generiert: *{}".format(cookie.get("value")[-6:]))
                return True
            else:
                self.log.error("Cookies können nicht erstellt werden!")
                return False
        except:
            return False


    def driver_book_appointment(self, driver, plz_impfzentrum):
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filepath = os.path.join(self.PATH, "tools\\log\\")
        url = f"{self.domain}impftermine/service?plz={plz_impfzentrum}"

        self.driver_enter_code(driver, plz_impfzentrum)

        try:
            # Klick auf "Termin suchen"
            button_xpath = "/html/body/app-root/div/app-page-its-search/div/div/div[2]/div/div/div[5]/div/div[1]/div[2]/div[2]/button"
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()
        except:
            self.log.error("Termine können nicht gesucht werden")
            try:
                driver.save_screenshot(filepath + "errorterminsuche" + timestamp + ".png")
            except:
                self.log.error("Screenshot konnte nicht gespeichert werden")
            pass

        # Termin auswählen
        try:
            time.sleep(3)
            button_xpath = '//*[@id="itsSearchAppointmentsModal"]/div/div/div[2]/div/div/form/div[1]/div[2]/label/div[2]/div'
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()
            time.sleep(.5)
        except:
            self.log.error("Termine können nicht ausgewählt werden")
            try:
                with open(filepath + "errorterminauswahl" + timestamp + ".html", 'w', encoding='utf-8') as file:
                    file.write(str(driver.page_source))
                driver.save_screenshot(filepath + "errorterminauswahl" + timestamp + ".png")
            except:
                self.log.error("HTML und Screenshot konnten nicht gespeichert werden")
            pass

        # Klick Button "AUSWÄHLEN"
        try:
            button_xpath = '//*[@id="itsSearchAppointmentsModal"]/div/div/div[2]/div/div/form/div[2]/button[1]'
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()
            time.sleep(.5)
        except:
            self.log.error("Termine können nicht ausgewählt werden (Button)")
            pass

        # Klick Daten erfassen
        try:
            button_xpath = '/html/body/app-root/div/app-page-its-search/div/div/div[2]/div/div/div[5]/div/div[2]/div[2]/div[2]/button'
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()
            time.sleep(.5)
        except:
            self.log.error("1. Daten können nicht erfasst werden")
            pass
        try:
            # Klick Anrede
            if self.kontakt['anrede'] == "Herr":
                button_xpath = '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[1]/div/div/div/label[1]/span'
            elif self.kontakt['anrede'] == "Frau":
                button_xpath = '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[1]/div/div/div/label[2]/span'
            else:
                button_xpath = '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[1]/div/div/div/label[3]/span'
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()

            # Input Vorname
            input_xpath = '/html/body/app-root/div/app-page-its-search/app-its-search-contact-modal/div/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[2]/div[1]/div/label/input'
            input_field = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, input_xpath)))
            action.move_to_element(input_field).click().perform()
            input_field.send_keys(self.kontakt['vorname'])

            # Input Nachname
            input_field = driver.find_element_by_xpath(
                '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[2]/div[2]/div/label/input')
            input_field.send_keys(self.kontakt['nachname'])

            # Input PLZ
            input_field = driver.find_element_by_xpath(
                '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[3]/div[1]/div/label/input')
            input_field.send_keys(self.kontakt['plz'])

            # Input City
            input_field = driver.find_element_by_xpath(
                '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[3]/div[2]/div/label/input')
            input_field.send_keys(self.kontakt['ort'])

            # Input Strasse
            input_field = driver.find_element_by_xpath(
                '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[4]/div[1]/div/label/input')
            input_field.send_keys(self.kontakt['strasse'])

            # Input Hasunummer
            input_field = driver.find_element_by_xpath(
                '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[4]/div[2]/div/label/input')
            input_field.send_keys(self.kontakt['hausnummer'])

            # Input Telefonnummer
            input_field = driver.find_element_by_xpath(
                '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[4]/div[3]/div/label/div/input')
            input_field.send_keys(self.kontakt['phone'].replace("+49", ""))

            # Input Mail
            input_field = driver.find_element_by_xpath(
                '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[1]/app-booking-contact-form/div[5]/div/div/label/input')
            input_field.send_keys(self.kontakt['notificationReceiver'])
        except:
            self.log.error("Kontaktdaten können nicht eingegeben werden")
            try:
                driver.save_screenshot(filepath + "errordateneingeben" + timestamp + ".png")
            except:
                self.log.error("Screenshot konnte nicht gespeichert werden")
            pass

        # Klick Button "ÜBERNEHMEN"
        try:
            button_xpath = '//*[@id="itsSearchContactModal"]/div/div/div[2]/div/form/div[2]/button[1]'
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()
            time.sleep(.7)
        except:
            self.log.error("Button ÜBERNEHMEN kann nicht gedrückt werden")
            pass

        # Termin buchen
        try:
            button_xpath = '/html/body/app-root/div/app-page-its-search/div/div/div[2]/div/div/div[5]/div/div[3]/div[2]/div[2]/button'
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()
        except:
            self.log.error("Button Termin buchen kann nicht gedrückt werden")
            pass
        time.sleep(3)
        if "Ihr Termin am" in str(driver.page_source):
            msg = "Termin erfolgreich gebucht!"
            self.log.success(msg)
            desktop_notification(operating_system=self.operating_system, title="Terminbuchung:", message=msg)
            return True
        else:
            self.log.error(
                "Automatisierte Terminbuchung fehlgeschlagen. Termin manuell im Fenster oder im Browser buchen.")
            print("Link für manuelle Buchung im Browser:", url)
            time.sleep(10 * 60)
            return False

    @retry_on_failure()
    def renew_cookies(self):
        """
        Cookies der Session erneuern, wenn sie abgelaufen sind.
        :return:
        """

        self.log.info("Browser-Cookies generieren")
        with self.get_chromedriver(headless=True) as driver:
            return self.driver_renew_cookies(driver, choice(self.plz_impfzentren))

          
    @retry_on_failure()
    def renew_cookies_code(self, manual=False):
        """
        Cookies der Session erneuern, wenn sie abgelaufen sind.
        :return:
        """

        self.log.info("Browser-Cookies generieren")
        with self.get_chromedriver(headless=False) as driver:
            return self.driver_renew_cookies_code(driver, choice(self.plz_impfzentren), manual)



    @retry_on_failure()
    def book_appointment(self):
        """
        Backup Prozess:
        Wenn die Terminbuchung mit dem Bot nicht klappt, wird das
        Browserfenster geöffnet und die Buchung im Browser beendet
        :return:
        """

        self.log.info("Termin über Selenium buchen")
        with self.get_chromedriver(headless=False) as driver:
            return self.driver_book_appointment(driver, self.plz_termin)

    @retry_on_failure()
    def login(self):
        """Einloggen mittels Code, um qualifizierte Impfstoffe zu erhalten.
        Dieser Schritt ist wahrscheinlich nicht zwingend notwendig, aber schadet auch nicht.

        :return: bool
        """

        path = f"rest/login?plz={choice(self.plz_impfzentren)}"

        res = self.s.get(self.domain + path, timeout=15)
        if res.ok:
            # Checken, welche Impfstoffe für das Alter zur Verfügung stehen
            self.qualifikationen = res.json().get("qualifikationen")

            if self.qualifikationen:
                zugewiesene_impfstoffe = set()

                for q in self.qualifikationen:
                    for verfuegbare_q in self.verfuegbare_qualifikationen:
                        if verfuegbare_q["qualification"] == q:
                            zugewiesene_impfstoffe.update(verfuegbare_q["impfstoffe"])

                self.log.info("Erfolgreich mit Code eingeloggt")
                self.log.info(f"Mögliche Impfstoffe: {list(zugewiesene_impfstoffe)}")
                print(" ")

                return True
            else:
                self.log.warn("Keine qualifizierten Impfstoffe verfügbar")
        else:
            return False

    def termin_valid(self, zeitpunkt):
        """Hier wird geprüft ob ein einzelner Termin gültig ist.
        """
        öffnung = datetime.fromisoformat('2021-06-07T00:02:00')
        return zeitpunkt >= öffnung

    @retry_on_failure()
    def termin_suchen(self, plz):
        """Es wird nach einen verfügbaren Termin in der gewünschten PLZ gesucht.
        Ausgewählt wird der erstbeste Termin (!).
        Zurückgegeben wird das Ergebnis der Abfrage und der Status-Code.
        Bei Status-Code > 400 müssen die Cookies erneuert werden.

        Beispiel für ein Termin-Paar:

        [{
            'slotId': 'slot-56817da7-3f46-4f97-9868-30a6ddabcdef',
            'begin': 1616999901000,
            'bsnr': '005221080'
        }, {
            'slotId': 'slot-d29f5c22-384c-4928-922a-30a6ddabcdef',
            'begin': 1623999901000,
            'bsnr': '005221080'
        }]

        :return: bool, status-code
        """

        path = f"rest/suche/impfterminsuche?plz={plz}"

        while True:
            res = self.s.get(self.domain + path, timeout=15)
            if not res.ok or 'Virtueller Warteraum des Impfterminservice' not in res.text:
                break
            self.log.info('Warteraum... zZz...')
            time.sleep(30)

        if res.ok:
            res_json = res.json()
            terminpaare = res_json.get("termine")
            terminpaare_mapped = list(map(
                lambda terminpaar : list(map(
                    lambda termin : {
                        **termin,
                        'beginTs': datetime.fromtimestamp(termin["begin"] / 1000)
                    },
                    terminpaar,
                )),
                terminpaare,
            ))
            terminpaare_filtered = list(filter(
                lambda terminpaar : all(map(lambda x:self.termin_valid(x['beginTs']), terminpaar)),
                terminpaare_mapped,
            ))
            if terminpaare:
                if not terminpaare_filtered:
                    self.log.info("Termine gefunden, aber außerhalb des angefragten Zeitrahmens.")
                    for n, terminpaar in enumerate(terminpaare_mapped, 1):
                        self.log.info(f"{n}. Terminpaar")
                        for num, termin in enumerate(terminpaar, 1):
                            ts = termin["beginTs"].strftime(
                                '%d.%m.%Y um %H:%M Uhr')
                            self.log.info(f"{num}. Termin: {ts}")
                else:
                    # Auswahl des erstbesten Terminpaares
                    self.terminpaar = choice(terminpaare_filtered)
                    self.plz_termin = plz
                    self.log.success(f"Terminpaar gefunden!")
                    self.impfzentrum = self.verfuegbare_impfzentren.get(plz)
                    self.log.success("'{}' in {} {}".format(
                        self.impfzentrum.get("Zentrumsname").strip(),
                        self.impfzentrum.get("PLZ"),
                        self.impfzentrum.get("Ort")))
                    for num, termin in enumerate(self.terminpaar, 1):
                        ts = datetime.fromtimestamp(termin["begin"] / 1000).strftime(
                            '%d.%m.%Y um %H:%M Uhr')
                        self.log.success(f"{num}. Termin: {ts}")
                    if ENABLE_BEEPY:
                        beepy.beep('coin')
                    else:
                        print("\a")
                    return True, 200
            else:
                self.log.info(f"Keine Termine verfügbar in {plz}")
        else:
            self.log.error(f"Terminpaare können nicht geladen werden: {res.text}")
        return False, res.status_code

    @retry_on_failure()
    def termin_buchen(self):
        """Termin wird gebucht für die Kontaktdaten, die beim Starten des
        Programms eingetragen oder aus der JSON-Datei importiert wurden.

        :return: bool
        """

        path = "rest/buchung"

        # Daten für Impftermin sammeln
        data = {
            "plz": self.plz_termin,
            "slots": [termin.get("slotId") for termin in self.terminpaar],
            "qualifikationen": self.qualifikationen,
            "contact": self.kontakt
        }

        res = self.s.post(self.domain + path, json=data, timeout=15)

        if res.status_code == 201:
            msg = "Termin erfolgreich gebucht!"
            self.log.success(msg)
            desktop_notification(operating_system=self.operating_system, title="Terminbuchung:", message=msg)
            return True

        elif res.status_code == 429:
            msg = "Anfrage wurde von der Botprotection geblockt. Cookies werden erneuert und die Buchung wiederholt."
            self.log.error(msg)
            self.renew_cookies_code()
            res = self.s.post(self.domain + path, json=data, timeout=15)
            if res.status_code == 201:
                msg = "Termin erfolgreich gebucht!"
                self.log.success(msg)
                desktop_notification(operating_system=self.operating_system, title="Terminbuchung:", message=msg)
                return True
            else:
                return False

        elif res.status_code >= 400:
            data = res.json()
            try:
                error = data['errors']['status']
            except KeyError:
                error = ''
            if 'nicht mehr verfügbar' in error:
                msg = f"Diesen Termin gibts nicht mehr: {error}"
            else:
                msg = f"Termin konnte nicht gebucht werden: {data}"
        else:
            msg = f"Unbekannter Statuscode: {res.status_code}"

        self.log.error(msg)
        desktop_notification(operating_system=self.operating_system, title="Terminbuchung:", message=msg)
        return False

    @retry_on_failure()
    def code_anfordern(self, mail, telefonnummer, plz_impfzentrum, leistungsmerkmal):
        """
        SMS-Code beim Impfterminservice anfordern.

        :param mail: Mail für Empfang des Codes
        :param telefonnummer: Telefonnummer für SMS-Code, inkl. Präfix +49
        :param plz_impfzentrum: PLZ des Impfzentrums, für das ein Code erstellt werden soll
        :param leistungsmerkmal: gewählte Impfgruppe (bspw. L921)
        :return:
        """

        path = "rest/smspin/anforderung"

        data = {
            "email": mail,
            "leistungsmerkmal": leistungsmerkmal,
            "phone": telefonnummer,
            "plz": plz_impfzentrum
        }
        while True:
            res = self.s.post(self.domain + path, json=data, timeout=15)
            if res.ok:
                token = res.json().get("token")
                return token
            elif res.status_code == 429:
                self.log.error(
                    "Anfrage wurde von der Botprotection geblockt.\n"
                    "Die Cookies müssen manuell im Browser generiert werden.\n")
                self.renew_cookies_code(True)
            else:
                self.log.error(f"Code kann nicht angefragt werden: {res.text}")
                return None

    @retry_on_failure()
    def code_bestaetigen(self, token, sms_pin):
        """
        Bestätigung der Code-Generierung mittels SMS-Code

        :param token: Token der Code-Erstellung
        :param sms_pin: 6-stelliger SMS-Code
        :return:
        """

        path = f"rest/smspin/verifikation"
        data = {
            "token": token,
            "smspin": sms_pin

        }
        while True:
            res = self.s.post(self.domain + path, json=data, timeout=15)
            if res.ok:
                self.log.success("Der Impf-Code wurde erfolgreich angefragt, bitte prüfe deine Mails!")
                return True
            elif res.status_code == 429:
                self.log.error("Cookies müssen erneuert werden.")
                self.renew_cookies_code()
            else:
                self.log.error(f"Code-Verifikation fehlgeschlagen: {res.text}")
                return False

    @staticmethod
    def terminsuche(code: str, plz_impfzentren: list, kontakt: dict, PATH: str, check_delay: int = 30):
        """
        Workflow für die Terminbuchung.

        :param code: 14-stelliger Impf-Code
        :param plz_impfzentren: Liste mit PLZ von Impfzentren
        :param kontakt: Kontaktdaten der zu impfenden Person als JSON
        :param check_delay: Zeit zwischen Iterationen der Terminsuche
        :return:
        """

        its = ImpfterminService(code, plz_impfzentren, kontakt, PATH)
        its.renew_cookies()

        # login ist nicht zwingend erforderlich
        its.login()

        while True:
            termin_gefunden = False
            while not termin_gefunden:

                # durchlaufe jede eingegebene PLZ und suche nach Termin
                for plz in its.plz_impfzentren:
                    termin_gefunden, status_code = its.termin_suchen(plz)

                    # Durchlauf aller PLZ unterbrechen, wenn Termin gefunden wurde
                    if termin_gefunden:
                        break
                    # Cookies erneuern
                    elif status_code >= 400:
                        its.renew_cookies()
                    # Suche pausieren
                    if not termin_gefunden:
                        time.sleep(check_delay)

            # Programm beenden, wenn Termin gefunden wurde
            if its.termin_buchen():
                return True

            # Cookies erneuern und pausieren, wenn Terminbuchung nicht möglich war
            # Anschließend nach neuem Termin suchen
            if its.book_appointment():
                return True
