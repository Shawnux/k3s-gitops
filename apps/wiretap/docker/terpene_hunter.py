import os, re, requests

from seleniumbase import SB



WEBHOOK = os.getenv("DISCORD_WEBHOOK_TERPENE")

STORES = [

    {"name": "Berkshire Roots", "url": "https://dutchie.com/embedded-menu/berkshire-roots/menu"},

    {"name": "Stage One", "url": "https://dutchie.com/embedded-menu/stage-one-cannabis-washington-ave/menu"},

    {"name": "Silver Therapeutics", "url": "https://dutchie.com/embedded-menu/st-albany-ny/menu"},

    {"name": "Temescal Wellness (Medical)", "url": "https://dutchie.com/embedded-menu/temescal-wellness-pittsfield-medical/menu"},

    {"name": "Verilife Albany", "url": "https://www.iheartjane.com/embed/stores/3531/menu"},

    {"name": "Rise Halfmoon", "url": "https://www.iheartjane.com/embed/stores/6086/menu"}

]



def extract_metric(text, keyword):

    match = re.search(rf'([0-9]+\.[0-9]+)\s*%\s*{keyword}|{keyword}.*?([0-9]+\.[0-9]+)', text, re.IGNORECASE)

    return float(match.group(1) or match.group(2)) if match else 0.0



def run_terpene_hunter():

    print("--- 🌿 Terpene Hunter v14.0 (SeleniumBase UC) Booting ---")

    outlier_myrcene = {"n": "None", "s": "None", "m": 0.0, "p": 0}

    outlier_thc = {"n": "None", "s": "Temescal Wellness (Medical)", "t": 0.0, "p": 0}

    total_found = 0



    # THE BYPASS: uc=True (Undetected), xvfb=True (Virtual Monitor)

    with SB(uc=True, headless=False, xvfb=True) as sb:

        for shop in STORES:

            print(f"Scouting {shop['name']}...")

            try:

                # uc_open_with_reconnect specifically handles Cloudflare Turnstile checkboxes

                sb.uc_open_with_reconnect(shop['url'], 5)

                sb.sleep(4)

                

                # Smash Age Gates using SeleniumBase syntax

                for text in ["21", "Yes", "Agree", "I am 21"]:

                    if sb.is_element_visible(f"button:contains('{text}')"):

                        sb.click(f"button:contains('{text}')")

                        sb.sleep(2)



                # Scroll to force lazy-loading

                for _ in range(6):

                    sb.execute_script("window.scrollBy(0, 1500);")

                    sb.sleep(1.5)

                

                # Extract the entire DOM text

                page_text = sb.get_text("body")

                

                # Brute force split the text into chunks looking for prices

                chunks = re.split(r'\$([0-9]+)', page_text)

                for i in range(1, len(chunks), 2):

                    price = int(chunks[i])

                    block = chunks[i-1][-150:] + chunks[i+1][:150] # Grab text around the price

                    

                    if "flower" not in block.lower() and "pre-roll" not in block.lower(): continue

                    

                    # Extract Name (Heuristic: usually the text before the price)

                    lines = [line.strip() for line in chunks[i-1].split('\n') if line.strip()]

                    name = lines[-1] if lines else "Unknown"

                    if name == "Unknown": continue

                    

                    m_val = extract_metric(block, 'myrcene')

                    t_val = extract_metric(block, 'thc')

                    if m_val > 0 or t_val > 0: total_found += 1



                    if m_val > outlier_myrcene['m']:

                        outlier_myrcene = {"n": name, "s": shop['name'], "m": m_val, "p": price}

                    if "Temescal" in shop['name'] and t_val > outlier_thc['t']:

                        outlier_thc = {"n": name, "s": shop['name'], "t": t_val, "p": price}



                print(f"  [>] Extracted data via Optical UC Engine.")

            except Exception as e: print(f"  [!] Failed {shop['name']}: {e}")



    # --- REPORTING ---

    print(f"\n[i] Total valid profiles parsed across all stores: {total_found}")

    os.makedirs("/data", exist_ok=True)

    db_file = "/data/last_terpene_report.txt"

    try:

        with open(db_file, "r") as f: last_report = f.read().strip()

    except: last_report = ""



    report = ""

    if outlier_myrcene['m'] > 0.0:

        report = f"**🏆 Absolute Outlier (Myrcene):**\n• {outlier_myrcene['n']} ({outlier_myrcene['s']}) — **{outlier_myrcene['m']}%** Myrcene (${outlier_myrcene['p']})"

        title, color = "📊 Market Pulse (Highest Terpene Profile)", 3447003

    elif outlier_thc['t'] > 0.0:

        report = f"*No terpene data found. Falling back to preferred medical menu outlier.*\n\n**🔥 Top THC (Temescal Medical):**\n⭐ {outlier_thc['n']} — **{outlier_thc['t']}%** THC (${outlier_thc['p']})"

        title, color = "📊 Preferred Store Pulse (THC Outlier)", 15158332

    else:

        print("[✓] Zero optical data parsed. Menus may be empty. Staying silent.")

        return



    if report != last_report:

        print(f"[+] New data verified! Broadcasting.\n{report}")

        if WEBHOOK: requests.post(WEBHOOK, json={"embeds": [{"title": title, "description": report, "color": color}]})

        with open(db_file, "w") as f: f.write(report)

    else: print("[✓] Data identical to last run. Staying silent.")



if __name__ == "__main__":

    run_terpene_hunter()
