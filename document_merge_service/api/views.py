import mimetypes
from tempfile import NamedTemporaryFile

import requests
from django.conf import settings
from django.db.models import Q
from django.http import HttpResponse
from django.utils.encoding import smart_str
from docxtpl import RichText
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.generics import RetrieveAPIView

from . import engines, models, serializers
from .unoconv import Unoconv


def walk_nested(data, fn):
    if isinstance(data, dict):
        return {key: walk_nested(value, fn) for (key, value) in data.items()}
    if isinstance(data, list):
        return [walk_nested(value, fn) for value in data]
    return fn(data)


class TemplateView(viewsets.ModelViewSet):
    queryset = models.Template.objects
    serializer_class = serializers.TemplateSerializer
    filterset_fields = {"slug": ["exact"], "description": ["icontains", "search"]}
    ordering_fields = ("slug", "description")
    ordering = ("slug",)

    def get_queryset(self):
        queryset = super().get_queryset()

        if settings.GROUP_ACCESS_ONLY:
            queryset = queryset.filter(
                Q(group__in=self.request.user.groups or []) | Q(group__isnull=True)
            )

        return queryset

    @action(
        methods=["post"],
        detail=True,
        serializer_class=serializers.TemplateMergeSerializer,
    )
    def merge(self, request, pk=None):
        template = self.get_object()
        engine = engines.get_engine(template.engine, template.template)

        content_type, _ = mimetypes.guess_type(template.template.name)
        response = HttpResponse(
            content_type=content_type or "application/force-download"
        )
        extension = mimetypes.guess_extension(content_type)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        def transform(value):
            if isinstance(value, str) and "\n" in value:
                return RichText(value)
            return value

        data = walk_nested(serializer.data["data"], transform)

        response = engine.merge(data, response)
        convert = serializer.data.get("convert")

        if convert:
            if settings.UNOCONV_LOCAL:
                with NamedTemporaryFile("wb") as tmp:
                    tmp.write(response.content)
                    unoconv = Unoconv(
                        pythonpath=settings.UNOCONV_PYTHON,
                        unoconvpath=settings.UNOCONV_PATH,
                    )
                    result = unoconv.process(tmp.name, convert)
                extension = convert
                status = 500
                if result.returncode == 0:
                    status = 200
                response = HttpResponse(
                    content=result.stdout,
                    status=status,
                    content_type=result.content_type,
                )
            else:
                url = f"{settings.UNOCONV_URL}/unoconv/{convert}"
                requests_response = requests.post(url, files={"file": response.content})
                fmt = settings.UNOCONV_FORMATS[convert]
                extension = fmt["extension"]
                content_type = fmt["mime"]

                response = HttpResponse(
                    content=requests_response.content,
                    status=requests_response.status_code,
                    content_type=content_type,
                )

        filename = f"{template.slug}.{extension}"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class DownloadTemplateView(RetrieveAPIView):
    queryset = models.Template.objects
    lookup_field = "template"

    def retrieve(self, request, **kwargs):
        template = self.get_object()

        mime_type, _ = mimetypes.guess_type(template.template.name)
        extension = mimetypes.guess_extension(mime_type)
        content_type = mime_type or "application/force-download"

        response = HttpResponse(content_type=content_type)
        response["Content-Disposition"] = 'attachment; filename="%s"' % smart_str(
            template.slug + extension
        )
        response["Content-Length"] = template.template.size
        response.write(template.template.read())
        return response
