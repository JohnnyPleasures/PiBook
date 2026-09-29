#!/bin/bash

trap 'exit 0' INT TERM

echo
echo '========================================'
echo '       TESTE DOS BOTOES PIBOOK'
echo '========================================'
echo
echo 'Carrega nos botoes fisicos.'
echo 'Testa toque CURTO e LONGO nos dois.'
echo
echo 'Para terminar: carregar em PARAR.'
echo
echo 'A escuta...'
echo

journalctl \
    -f \
    -n 0 \
    -u pibook-zero.service \
    -o cat |
while IFS= read -r line; do
    case "$line" in

        *"GPIO Button 'toggle': SHORT PRESS"*)
            echo "GPIO5  Avancar/Seguinte  -> CURTO"
            ;;

        *"GPIO Button 'toggle': LONG PRESS"*)
            echo "GPIO5  Confirmar         -> LONGO"
            ;;

        *"GPIO Button 'back': SHORT PRESS"*)
            echo "GPIO6  Recuar/Anterior   -> CURTO"
            ;;

        *"GPIO Button 'back': LONG PRESS"*)
            echo "GPIO6  Voltar            -> LONGO"
            ;;

        *"GPIO Button "*)
            echo "$line"
            ;;
    esac
done
