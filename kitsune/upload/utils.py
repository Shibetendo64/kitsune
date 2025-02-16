import io
import os

from django.conf import settings
from django.core.files import File
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.utils.html import escape
from django.utils.translation import ugettext_lazy as _lazy
from PIL import Image

from kitsune.upload.forms import ImageAttachmentUploadForm
from kitsune.upload.models import ImageAttachment
from kitsune.upload.tasks import _scale_dimensions, compress_image, generate_thumbnail


def check_file_size(f, max_allowed_size):
    """Check the file size of f is less than max_allowed_size

    Raise FileTooLargeError if the check fails.

    """
    if f.size > max_allowed_size:
        message = _lazy('"%s" is too large (%sKB), the limit is %sKB') % (
            f.name,
            f.size >> 10,
            max_allowed_size >> 10,
        )
        raise FileTooLargeError(message)


def create_imageattachment(files, user, obj):
    """
    Given an uploaded file, a user and an object, it creates an ImageAttachment
    owned by `user` and attached to `obj`.
    """
    up_file = list(files.values())[0]
    check_file_size(up_file, settings.IMAGE_MAX_FILESIZE)

    (up_file, is_animated) = _image_to_png(up_file)

    image = ImageAttachment(content_object=obj, creator=user)
    image.file.save(up_file.name, File(up_file), save=True)

    # Compress and generate thumbnail off thread
    generate_thumbnail.delay(image, "file", "thumbnail")
    if not is_animated:
        compress_image.delay(image, "file")

    # Refresh because the image may have been changed by tasks.
    image.refresh_from_db()

    (width, height) = _scale_dimensions(image.file.width, image.file.height)

    # The filename may contain html in it. Escape it.
    name = escape(up_file.name)

    return {
        "name": name,
        "url": image.file.url,
        "thumbnail_url": image.thumbnail_if_set().url,
        "width": width,
        "height": height,
        "delete_url": image.get_delete_url(),
    }


def _image_to_png(up_file):
    # Extract exif data
    original_image = Image.open(up_file)
    original_image_data = list(original_image.getdata())
    pil_image = Image.new(original_image.mode, original_image.size)
    pil_image.putdata(original_image_data)

    # Detect animated GIFS since we don't convert them.
    try:
        # TODO: Find a less memory intensive way to do this, if even possible.
        image_animation_check = pil_image
        image_animation_check.seek(1)
    except EOFError:
        is_animated = False
    else:
        is_animated = True

    if not is_animated:
        converted_image = io.BytesIO()
        options = {}
        if "transparency" in pil_image.info:
            options["transparency"] = pil_image.info["transparency"]
        pil_image.save(converted_image, format="PNG", **options)

        up_file = InMemoryUploadedFile(
            converted_image,
            None,
            os.path.splitext(up_file.name)[0] + ".png",
            "image/png",
            len(converted_image.getbuffer()),
            None,
        )

    return (up_file, is_animated)


class FileTooLargeError(Exception):
    pass


def upload_imageattachment(request, obj):
    """Uploads image attachments. See upload_media.

    Attaches images to the given object, using the create_imageattachment
    callback.

    """
    return upload_media(request, ImageAttachmentUploadForm, create_imageattachment, obj=obj)


def upload_media(request, form_cls, up_file_callback, instance=None, **kwargs):
    """
    Uploads media files and returns a list with information about each media:
    name, url, thumbnail_url, width, height.

    Args:
    * request object
    * form class, used to instantiate and validate form for upload
    * callback to save the file given its content and creator
    * extra kwargs will all be passed to the callback

    """
    form = form_cls(request.POST, request.FILES)
    if request.method == "POST" and form.is_valid():
        return up_file_callback(request.FILES, request.user, **kwargs)
    elif not form.is_valid():
        return form.errors
    return None
