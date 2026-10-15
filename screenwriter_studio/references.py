"""Reference settings shared by the desk, prompt writers and image renderer."""

REFERENCE_MODES = ("style", "character", "location", "object", "composition", "lock")


def reference_choices(references) -> list[dict]:
    """Validate settings; bare paths and old Lock selections retain their meaning."""
    if references is None:
        return []
    if not isinstance(references, list):
        raise ValueError("Reference images must be chosen from the library.")
    chosen = []
    seen = set()
    for raw in references:
        item = raw if isinstance(raw, dict) else {"path": raw}
        path, mode = item.get("path"), item.get("mode", "lock")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Reference images must be chosen from the library.")
        if mode not in REFERENCE_MODES:
            raise ValueError("Choose a valid reference role: style, character, location, object or composition.")
        result = {"path": path.strip(), "mode": mode}
        for key, limit in (("name", 120), ("instruction", 1000)):
            if key in item:
                if not isinstance(item[key], str) or len(item[key]) > limit:
                    raise ValueError(f"Reference {key} must be text of at most {limit} characters.")
                result[key] = item[key].strip()
        scope = item.get("scope", "shots" if item.get("shots") else "auto")
        shots = item.get("shots", [])
        if scope not in ("auto", "shots"):
            raise ValueError("Reference scope must be auto or shots.")
        if (not isinstance(shots, list) or len(shots) > 240
                or any(type(n) is not int or not 1 <= n <= 240 for n in shots)):
            raise ValueError("Reference shots must be numbers between 1 and 240.")
        if (scope == "shots" and not shots) or (scope == "auto" and shots):
            raise ValueError("Choose shot numbers for a specific-shot reference, or use automatic scope.")
        if "scope" in item or "shots" in item:
            result.update(scope=scope, shots=sorted(set(shots)))
        if mode == "character":
            for key in ("face", "wardrobe"):
                if type(item.get(key, True)) is not bool:
                    raise ValueError(f"Character {key} must be on or off.")
                result[key] = item.get(key, True)
            if not result["face"] and not result["wardrobe"]:
                raise ValueError("Choose Face, Wardrobe, or both for a character reference.")
        if result["path"] not in seen:
            chosen.append(result)
            seen.add(result["path"])
    return chosen


def reference_direction(item: dict) -> str:
    """Describe only the attributes the user chose to preserve."""
    mode = item.get("mode", "lock")
    if mode == "character":
        face = "Keep the face and identifying appearance." if item.get("face", True) else "Do not copy the face or identity."
        wardrobe = "Keep the wardrobe." if item.get("wardrobe", True) else "Use the scripted wardrobe; do not copy the reference clothing."
        direction = (f"Character: {face} {wardrobe} Do not copy the background or composition. "
                     "Where the prompt describes a change (clothing removed or added, dirt, injuries), follow the prompt.")
    else:
        direction = {
            "style": "Style: match colour grade, light, texture and mood only. Do not copy people, wardrobe, places, objects or composition.",
            "location": "Location: keep the setting, architecture and spatial details. Do not copy people, wardrobe or camera framing.",
            "object": "Object / Product: keep the object's shape, materials and identifying details. Do not copy people, background or composition.",
            "composition": "Composition: follow framing, camera angle and subject placement only. Use the scripted people, wardrobe, objects and location.",
            "lock": "Lock: keep the depicted faces, costumes and places only where the story shows them.",
        }[mode]
    if item.get("instruction"):
        direction += f" Additional direction within this role: {item['instruction']}"
    return direction


def validate_reference_shots(references: list[dict], shots: list[dict]) -> None:
    available = {shot["number"] for shot in shots}
    for ref in references:
        missing = set(ref.get("shots", [])) - available
        if missing:
            numbers = ", ".join(str(n) for n in sorted(missing))
            raise ValueError(f"Reference {ref.get('name') or ref['id']} selects missing shots: {numbers}. No images were submitted.")


def story_role(item: dict) -> str:
    """What a reference supplies, in the words the story crew needs. Image directions stay out."""
    mode = item.get("mode", "lock")
    if mode == "character":
        parts = [part for part, on in (("face", item.get("face", True)), ("wardrobe", item.get("wardrobe", True))) if on]
        supplied = "Character: the picture supplies the " + " and ".join(parts) + "."
        if len(parts) == 1:
            supplied += f" Describe the {'wardrobe' if parts == ['face'] else 'face and appearance'} in the lock."
    else:
        supplied = {
            "style": "Style: look only. It adds no character, place or object.",
            "location": "Location: the picture supplies the place.",
            "object": "Object / Product: the picture supplies the object.",
            "composition": "Composition: framing only. It adds no character, place or object.",
            "lock": "Locked: keep the faces, costumes and places it shows.",
        }[mode]
    if item.get("instruction"):
        supplied += f" The user adds: {item['instruction']}"
    return supplied


def _key(name: str) -> str:
    key = " ".join(str(name or "").casefold().split())
    return key[4:] if key.startswith("the ") else key


def covered_sheets(references, characters: list[str], locations: list[str]) -> int:
    """Character and location sheets the art director need not make, because a reference already shows them.

    A character reference covers its character when it keeps both face and wardrobe, applies
    automatically, and its name (or first name) is the character's name. A location reference
    covers the location that begins with its name. Legacy Lock references count one each.
    """
    count = sum(1 for item in references or [] if item.get("mode", "lock") == "lock")
    names = [_key(name) for name in characters]
    places = [_key(place) for place in locations]
    covered = set()
    for item in references or []:
        key = _key(item.get("name", ""))
        if not key or item.get("shots"):
            continue
        if item.get("mode") == "character" and item.get("face", True) and item.get("wardrobe", True):
            covered.update(("character", n) for n, name in enumerate(names) if name in (key, key.split()[0]))
        elif item.get("mode") == "location":
            covered.update(("location", n) for n, place in enumerate(places)
                           if place.startswith(key) and not place[len(key):len(key) + 1].isalnum())
    return min(len(names) + len(places), count + len(covered))
