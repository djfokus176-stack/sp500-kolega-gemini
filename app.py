✅ Przyjmuję ten plan jako podstawę dalszych prac nad SP500 SIGNAL AI PRO. Nie zmieniamy strategii LONG/SHORT, nie uruchamiamy prawdziwych transakcji i nie kasujemy dotychczasowych wyników PAPER.

Ustalamy następującą kolejność:
Wersja	Co poprawiamy	Priorytet
V16.2	Odczyty Capital.com, świeżość M1/M5/M15/M30/H1, szczegółowe komunikaty błędów	🔴 Pilne
V16.3	Trwałe archiwum, zabezpieczenie przycisków, odzyskiwanie historii, status WYGASŁY	🔴 Pilne
V17	Testy nowych filtrów i porównanie strategii	🟡 Dopiero później


Dwie ważne zasady techniczne
1. Nie wygaszamy prognoz tylko dlatego, że minęły 24 godziny.

Najpierw program powinien spróbować odzyskać historyczne świece. Dopiero kiedy nie można już wiarygodnie rozliczyć próby, otrzyma ona status WYGASŁY. Taka próba nie będzie liczona ani jako trafiona, ani jako przegrana.

2. Nie wystarczy samo włączenie Supabase.

Musimy również sprawdzić, czy synchronizacja rzeczywiście działa, dane można odzyskać po restarcie i czy nieuprawnieni użytkownicy nie mogą dopisywać prób do archiwum.
🔒 Zasady aktualizacji GitHub
Każda poprawka będzie przygotowana i testowana osobno. Przed scaleniem sprawdzimy testy automatyczne i zgodność z dotychczasowymi danymi.

Nie wykonamy kolejnego merge przed zabezpieczeniem Twojej lokalnej historii.

Najpierw pobierz w aplikacji:

📥 BACKUP CAŁEJ LOKALNEJ HISTORII (CSV)

Nie wklejaj żadnych kluczy API ani haseł.
Co jest ustalone na teraz?
Mamy zakończony wstępny audyt V16.1 i zidentyfikowane obszary wymagające naprawy. V16.2 i V16.3 nie zostały jeszcze wdrożone.

Następnym etapem będzie przygotowanie poprawki diagnostycznej V16.2 w osobnej gałęzi GitHub, bez scalania do main. W ten sposób zachowamy obecną wersję programu i będziemy mogli bezpiecznie porównać działanie przed zmianami i po nich.
