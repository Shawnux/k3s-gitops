import os, re, requests

from seleniumbase import SB



WEBHOOK = os.getenv("DISCORD_WEBHOOK_EDM")

TARGETS = ["Ganja White Night", "Subtronics", "Zeds Dead", "Excision", "Rezz", "LSDREAM", "CloZee", "Tape B", "Levity", "Ame", "Bicep"]



def run_edm_hunter():

    print("--- 🎧 EDM Hunter v14.0 (SeleniumBase UC) Booting ---")

    new_shows = 0

    os.makedirs("/data", exist_ok=True)

    db_file = "/data/last_edm_shows.txt"

    try:

        with open(db_file, "r") as f: known_shows = set(f.read().splitlines())

    except: known_shows = set()



    sources = [

        {"name": "EDMTrain NY", "url": "https://edmtrain.com/new-york", "container": ".event-list-item, .event"},

        {"name": "Resident Advisor", "url": "https://ra.co/events/us/newyork", "container": "li"}

    ]



    with SB(uc=True, headless=False, xvfb=True) as sb:

        for src in sources:

            print(f"  [+] Scouting {src['name']}...")

            try:

                sb.uc_open_with_reconnect(src['url'], 5)

                sb.sleep(3)

                

                for _ in range(5):

                    sb.execute_script("window.scrollBy(0, 1500);")

                    sb.sleep(1)



                # Extract all event containers visible on the page

                containers = sb.find_elements(src['container'])

                for container in containers:

                    text = container.text

                    text_lower = text.lower().replace('â', 'a')

                    

                    for art in TARGETS:

                        norm_art = art.lower().replace('â', 'a')

                        if re.search(rf'\b{re.escape(norm_art)}\b', text_lower):

                            try:

                                link_el = container.find_element("css selector", "a")

                                full_url = link_el.get_attribute("href")

                                

                                e_id = f"{src['name'][:2]}_{art.replace(' ','')}_{full_url.split('/')[-1]}"

                                if e_id not in known_shows:

                                    known_shows.add(e_id)

                                    new_shows += 1

                                    

                                    clean_desc = "\n".join([line for line in text.split('\n') if len(line) > 2][:6])

                                    msg = f"**Source:** {src['name']}\n**Details:**\n{clean_desc}\n\n[🎟️ View Event & Tickets]({full_url})"

                                    if WEBHOOK: requests.post(WEBHOOK, json={"embeds": [{"title": f"🚨 NEW SHOW: {art}", "description": msg, "color": 15158332}]})

                            except: pass

            except Exception as e: print(f"  [!] Failed {src['name']}: {e}")



    with open(db_file, "w") as f: f.write("\n".join(known_shows))

    if new_shows == 0: print("[✓] No new target shows found. Staying silent.")

    else: print(f"[+] Broadcasted {new_shows} full-context event snippets.")



if __name__ == "__main__":

    run_edm_hunter()
