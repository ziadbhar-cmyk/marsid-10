وكيل مزامنة مرصد الأعمال — MarsadAgent
==========================================

الوكيل يشتغل على الجهاز الذي فيه قاعدة بيانات المنظومة (السيرفر المحلي في المحل أو المصنع).
يقرأ البيانات فقط ويرسلها للسيرفر السحابي. لا يعدّل أي شيء في المنظومة.

التركيب
-------
1) ثبّت Python 3.11 أو أحدث (مرة واحدة) من python.org
   - اختر Install for all users
   - فعّل Add python.exe to PATH
2) فك ضغط هذا المجلد في C:\MarsadAgent
3) افتح config.json بالمفكرة (Notepad) وعدّل قسم "database" فقط
4) شغّل setup_agent.bat بزر الفأرة الأيمن ← Run as administrator
5) ارجع لصفحة الوكيل في الموقع، وستظهر حالة "متصل"

أمثلة config.json → database
-----------------------------
SQL Server (أغلب المنظومات المحلية):
  "driver": "sqlserver",
  "connection_string": "DRIVER={SQL Server};SERVER=.\\SQLEXPRESS;DATABASE=AbuGhris2024;UID=marsad_read;PWD=StrongPass#1"

  - SERVER: اسم السيرفر كما يظهر في شاشة دخول المنظومة أو SQL Server Management Studio
    مثل  .\SQLEXPRESS  أو  localhost  أو  PC-NAME\SQL2019
  - داخل JSON اكتب الشرطة المائلة مرتين  \\
  - لو برايفر {SQL Server} قديم ولم يعمل جرّب {ODBC Driver 17 for SQL Server}

Microsoft Access:
  "driver": "access",
  "connection_string": "DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=C:\\Program Files\\System\\Data.mdb;PWD=;"

MySQL:  "driver": "mysql", "host": "127.0.0.1", "port": 3306, "database": "db", "user": "u", "password": "p"
Firebird: "driver": "firebird", "dsn": "127.0.0.1:C:\\Data\\DATA.FDB", "user": "SYSDBA", "password": "masterkey"

إنشاء مستخدم قراءة فقط في SQL Server (مستحسن جداً)
----------------------------------------------------
افتح SQL Server Management Studio ونفذ (غيّر اسم القاعدة وكلمة المرور):

  CREATE LOGIN marsad_read WITH PASSWORD = 'StrongPass#1', CHECK_POLICY = OFF;
  USE AbuGhris2024;
  CREATE USER marsad_read FOR LOGIN marsad_read;
  ALTER ROLE db_datareader ADD MEMBER marsad_read;

وتأكد أن SQL Server يقبل دخول SQL Authentication (Mixed Mode).

ملاحظات
-------
- الوكيل يشتغل تلقائياً كل 5 دقائق ومع إعادة تشغيل الجهاز (مهمة MarsadAgent في Task Scheduler).
- لو انقطع الإنترنت يحفظ البيانات في مجلد outbox ويرسلها عند رجوع الاتصال.
- السجل في agent.log
- للمراقبة المباشرة شغّل run_once_visible.bat
- للإزالة شغّل uninstall_agent.bat
- راجع عقد مزود المنظومة: بعض المزودين يشترطون موافقتهم على القراءة من القاعدة.
