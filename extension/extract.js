// Reads the LinkedIn profile you are looking at. It runs inside that tab only when the side
// panel asks (when you open a profile with the panel open, or click "Read again"). It reads
// what is already on the page - no scrolling, no clicks, no extra requests to LinkedIn.
//
// LinkedIn renames its CSS classes and reshuffles its markup often, so this reads the page by
// its visible text: the h1 is the name, the lines under it are the headline and location, and
// the Experience section is parsed line by line around its date ranges ("Apr 2024 - Present").
// eslint-disable-next-line no-unused-vars
function qlExtractProfile() {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const url = window.location.href.split(/[?#]/)[0];
  if (!/linkedin\.com\/in\//.test(url)) return { error: "Not a LinkedIn profile page" };

  // Visible text lines of an element, top to bottom, without screen-reader duplicates.
  function lines(root) {
    if (!root) return [];
    const out = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const el = node.parentElement;
        if (!el || !clean(node.textContent)) return NodeFilter.FILTER_REJECT;
        if (el.closest("script, style, svg, noscript, .visually-hidden, .a11y-text, [hidden]"))
          return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let node;
    while ((node = walker.nextNode())) {
      const text = clean(node.textContent);
      if (out[out.length - 1] !== text) out.push(text);
    }
    return out;
  }

  const main = document.querySelector("main") || document.body;
  const h1 = main.querySelector("h1") || document.querySelector("h1");
  let fullName = clean(h1 && h1.textContent);
  if (!fullName) fullName = clean(document.title.replace(/^\(\d+\)\s*/, "").split("|")[0]);

  // ---------- top card: the section that holds the h1
  const card = (h1 && (h1.closest("section") || h1.parentElement?.parentElement)) || main;
  const SKIP = [
    /^(he|she|they|ze)\s*\/\s*\w+/i,
    /^·?\s*(1st|2nd|3rd\+?)( degree connection)?$/i,
    /^·$/, /^(verified|premium|open to work|hiring|following|follow)$/i,
    /^\d[\d,+]* (followers|connections)/i, /^contact info$/i,
    /^(message|connect|more|pending|view in sales navigator|visit my website)$/i,
  ];
  const cardLines = lines(card).filter((l) => l !== fullName);
  const headline = cardLines.filter((l) => !SKIP.some((re) => re.test(l))).find((l) => l.length > 2) || "";

  let place = "";
  const ci = cardLines.findIndex((l) => /contact info/i.test(l));
  if (ci >= 0) {
    place = clean(cardLines[ci].replace(/·?\s*contact info/i, "").replace(/·$/, ""));
    for (let i = ci - 1; !place && i >= 0 && i >= ci - 3; i--) place = clean(cardLines[i].replace(/^·|·$/g, ""));
  }
  let buttonCompany = "";
  const companyBtn = card.querySelector("[aria-label^='Current company']");
  if (companyBtn) {
    buttonCompany = clean(companyBtn.getAttribute("aria-label")
      .replace(/^Current company:\s*/i, "").replace(/\.\s*Click.*$/i, ""));
  }

  // ---------- sections, found by anchor id or by heading text
  function section(id, heading) {
    const anchor = document.getElementById(id);
    if (anchor) return anchor.closest("section") || anchor.parentElement;
    return [...document.querySelectorAll("section")].find((s) => {
      const h = s.querySelector("h2, h3");
      return h && clean(h.textContent).toLowerCase().startsWith(heading);
    });
  }

  const MON = "(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?";
  const DATE_RANGE = new RegExp(`^(${MON} )?\\d{4}\\s*[-–—]\\s*((${MON} )?\\d{4}|present)\\b`, "i");
  const DURATION = /^(\d+ yrs?( \d+ mos?)?|\d+ mos?|less than a year)$/i;
  const EMP = "(full-time|part-time|self-employed|freelance|internship|contract|apprenticeship|seasonal|trainee|permanent)";
  const EMP_ONLY = new RegExp(`^${EMP}$`, "i");
  const GROUP_META = new RegExp(`^(${EMP}\\s*·\\s*)?(\\d+ yrs?( \\d+ mos?)?|\\d+ mos?|less than a year)$`, "i");
  const PLACE_WORDS = /(\barea\b|remote|on-site|hybrid|india|united states|united kingdom|singapore|germany|canada|australia|metropolitan|region)/i;
  const COMPANY_SUFFIX = /\b(inc|llc|ltd|limited|corp|co|gmbh|plc|llp)\.?$/i;
  const PLACE = { test: (l) => !COMPANY_SUFFIX.test(l) && (PLACE_WORDS.test(l) || /\b[A-Z][a-z]+, [A-Z][a-z]+/.test(l)) };
  const NOISE = /^(experience|show all.*|see more|…see more|…more|skills:.*|\+\s?\d+ skills?|.* and \+\d+ skills?)$/i;
  const isDescription = (l) => l.length > 80 || /[.!?…]$/.test(l);

  // Parse the Experience section from its text. Each role has a date-range line; the title
  // and company sit in the one or two lines just above it. Several roles at one company are
  // grouped under a header line ("IQVIA") followed by a total duration ("5 yrs 11 mos").
  function parseExperience(sec) {
    const L = lines(sec).filter((l) => !NOISE.test(l));
    const links = [...sec.querySelectorAll("a[href*='/company/']")].map((a) => ({
      href: a.href.split(/[?#]/)[0],
      text: (lines(a).join(" ") + " " + [...a.querySelectorAll("img[alt]")].map((i) => i.alt).join(" ")).toLowerCase(),
    }));
    const urlFor = (company) => {
      const c = company.toLowerCase();
      const hit = c && links.find((l) => l.text.includes(c));
      return hit ? hit.href : "";
    };

    const roles = [];
    let group = null, groupIdx = -1, prevEnd = -1;
    for (let d = 0; d < L.length; d++) {
      if (!DATE_RANGE.test(L[d])) continue;
      // a group header between the previous role and this one?
      for (let i = prevEnd + 1; i < d - 1; i++) {
        if (GROUP_META.test(L[i + 1]) && !DATE_RANGE.test(L[i]) && !EMP_ONLY.test(L[i])) {
          group = L[i]; groupIdx = i + 1;
        }
      }
      const floor = Math.max(prevEnd, groupIdx);
      let x = d - 1;
      if (x > floor && EMP_ONLY.test(L[x])) x--;               // "Full-time" under a grouped title
      if (x <= floor) { prevEnd = d; continue; }
      const X = L[x], y = x - 1, Y = y > floor ? L[y] : null;
      let title, company;
      if (X.includes(" · ") && Y) {                             // "Blue Matter · Full-time"
        title = Y; company = X.split(" · ")[0]; group = null;
      } else if (group && (!Y || isDescription(Y) || (y === floor + 1 && PLACE.test(Y)))) {
        title = X; company = group;                             // role inside a group
      } else if (Y && !isDescription(Y) && !(y === prevEnd + 1 && PLACE.test(Y))) {
        title = Y; company = X; group = null;                   // "Advisor" / "Aintu, Inc."
      } else {
        title = X; company = group || "";
      }
      const dates = clean(L[d].split(" · ")[0]);
      roles.push({ title: clean(title), company: clean(company), dates,
                   current: /present/i.test(dates), company_url: urlFor(clean(company)) });
      prevEnd = d;
    }
    return roles.slice(0, 8);
  }

  const expSection = section("experience", "experience");
  const experience = expSection ? parseExperience(expSection) : [];

  let about = "";
  const ab = section("about", "about");
  if (ab) about = lines(ab).filter((l) => !/^about$/i.test(l) && !/see more$/i.test(l)).sort((a, b) => b.length - a.length)[0] || "";

  // The latest role wins: the first one still "Present", else the first one listed.
  const current = experience.find((e) => e.current) || experience[0];
  let company = (current && current.company) || buttonCompany;
  if (!company) {
    const m = headline.match(/\s(?:at|@)\s+(.+?)(\s[|•·]\s|$)/i);
    if (m) company = clean(m[1]);
  }
  const title = current && current.company === company ? current.title : "";
  const companyUrl = (current && current.company === company && current.company_url) || "";

  return {
    full_name: fullName, headline, title, company_name: company, location: place,
    about: about.slice(0, 1500), experience, company_linkedin_url: companyUrl, linkedin_url: url,
  };
}
