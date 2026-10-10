"""Small production fixtures shared by the tests."""

from screenwriter_studio.schemas import Beat, CharacterLock, Development, Screenplay, Shot


def _piece() -> tuple[Development, Screenplay]:
    development = Development(
        title="Hall",
        slug="hall",
        job_type="narrative",
        runtime_seconds=30,
        aspect_ratio="16:9",
        language="English",
        tone="quiet",
        audience="adults",
        logline="When the light fails, Maya must cross the hall before dawn.",
        beats=[
            Beat(name="Hook", timecode="0:00", action="Maya waits at the door."),
            Beat(name="Turn", timecode="0:10", action="The light goes out."),
            Beat(name="Button", timecode="0:20", action="She reaches the far door."),
        ],
        look="one window",
        palette="grey",
        lighting="practical",
        characters=[CharacterLock(name="Maya", description="A woman of 40 with short black hair.")],
        locations=["hall"],
        invented=[],
    )
    screenplay = Screenplay(
        fountain="Title: Hall\n\nINT. HALL - NIGHT\n\nMaya waits.\n",
        shots=[
            Shot(
                number=1,
                duration_seconds=8,
                framing="wide",
                action="Maya waits at the door.",
                audio="room tone",
                notes="",
            )
        ],
        notes=[],
    )
    return development, screenplay
