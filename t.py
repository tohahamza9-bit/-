import time
from pywinauto import Application

PID = 9576   # ← رقم النسخة الوحيدة من MONEYADO
app = Application(backend='win32').connect(process=PID)
form = app.window(class_name='ThunderRT6FormDC')

fields = form.descendants(class_name='ThunderRT6TextBox') \
       + form.descendants(class_name='ThunderRT6ComboBox')

for i, f in enumerate(fields):
    r = f.rectangle()
    print(f'#{i}  coords={r}')      # ASCII فقط — يبان صح في الطرفية
    f.draw_outline(colour='red', thickness=4)
    input('    شوف أي خانة صار عليها إطار أحمر، سجّل اسمها، ثم Enter للتالي...')
