# Imagem mínima que fica no lugar do kirocrew real (pesado, licenciado,
# com device-flow que exige um humano) só pro smoke-test em cluster
# efêmero -- exercita reconcile/CHP/NetworkPolicy do KrewHub, não o
# produto Kiro Crew em si (que já tem smoke-test manual ad-hoc do
# operador contra o cluster real, fora deste repo).
FROM python:3.13-alpine
COPY server.py /app/server.py
COPY kirocrew /usr/local/bin/kirocrew
RUN chmod +x /usr/local/bin/kirocrew
EXPOSE 5476
ENTRYPOINT ["python3", "/app/server.py"]
