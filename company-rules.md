# قوانین Code Review — Java / Spring Boot

> این فایل، نسخه‌ی کوتاه و اجرایی قوانین موجود در سند «استاندارد کدنویسی» است. در Code Review، موارد زیر را به‌عنوان قانون بررسی کن.

## 1. نام‌گذاری و ساختار

- نام کلاس‌ها را مفرد و مبتنی بر مفهوم دامنه انتخاب کن.
- برای Interface از پیشوندهایی مانند `I` استفاده نکن.
- نام Packageها را کاملاً lowercase و بدون جداکننده بنویس.
- نام کلاس را بر اساس نقش واقعی آن انتخاب کن، نه تکنولوژی مورد استفاده.
- از مخفف‌های غیرضروری مانند `Svc`، `Mgr` و `Util2` خودداری کن.
- اگر Service فقط یک پیاده‌سازی دارد، Interface جداگانه نساز.
- نام متد را با فعل شروع کن و رفتار آن را واضح بیان کن.
- `find` را فقط زمانی به‌کار ببر که نبودن نتیجه مجاز است؛ خروجی باید `Optional` یا مجموعه خالی باشد.
- `get` را فقط زمانی به‌کار ببر که نتیجه باید وجود داشته باشد؛ در نبود نتیجه Exception پرتاب کن.
- متدهای boolean را با `is`، `has` یا `can` شروع کن.
- برای عملیات پرهزینه یا فراخوانی شبکه از پیشوند `get` استفاده نکن؛ از نام‌هایی مانند `calculate` یا `fetch` استفاده کن.
- برای یک مفهوم واحد، در کل پروژه از یک فعل ثابت استفاده کن.

## 2. Lombok، DTO و Mapper

- برای getterهای ساده حتماً از Lombok `@Getter` استفاده کن و getter را دستی ننویس. Setter، Constructor و Builder تکراری را نیز دستی ننویس و از Lombok استفاده کن.
- روی Entity از `@Data` استفاده نکن اما انوتیشن های دیگر قابل استفاده اند.
- برای DTO خروجی ترجیحاً از `record` استفاده کن؛ در غیر این صورت آن را immutable نگه دار.
- برای سازنده‌هایی با پارامترهای زیاد از Builder استفاده کن.
- تبدیل بین Entity و DTO را با MapStruct انجام بده.
- در Mapper، `unmappedTargetPolicy = ReportingPolicy.ERROR` را فعال کن.
- فیلدهایی را که عمداً نباید نگاشت شوند، صریحاً با `ignore = true` مشخص کن.
- Entity را مستقیم به‌عنوان ورودی یا خروجی API استفاده نکن.

## 3. Null و Validation

- برای ورودی و خروجی متدهایی که نباید null باشند، `@NotNull` قرار بده.
- برای فعال شدن Validation روی پارامترهای متد، `@Validated` را روی کلاس قرار بده.
- Validation ورودی را با Annotation انجام بده، نه با شرط‌های دستی داخل Service.
- در Controller برای DTO ورودی از `@Valid` استفاده کن.
- قواعد دامنه تکرارشونده را در Annotation اختصاصی متمرکز کن.
- در Validator اختصاصی، بررسی null را با Validation مربوط به حضور مقدار ترکیب نکن.
- پیام Validation را به‌صورت کلید ثابت تعریف کن، نه متن قابل نمایش.
- از پارامتر nullable تا حد امکان پرهیز کن.
- از متدها `null` برنگردان؛ برای Collection مقدار خالی و برای مقدار تکی `Optional` برگردان.
- `null` را به متدی که null نمی‌پذیرد پاس نده.

## 4. Dependency Injection و Configuration

- در Service و Component از Constructor Injection استفاده کن.
- به‌جای Field Injection و `@Autowired` از `@RequiredArgsConstructor` و فیلدهای `final` استفاده کن.
- مقادیر قابل تغییر مانند نام Queue، اندازه Pool، Limit و Timeout را Hardcode نکن؛ از Property بخوان.
- Propertyهای مرتبط را در کلاس‌های `@ConfigurationProperties` گروه‌بندی کن.
- Token، Password، API Key و سایر Secretها را داخل کد یا فایل Version-Control شده قرار نده.

## 5. ساختار لایه‌ها و طراحی

- Controller را فقط مسئول دریافت درخواست، Validation و ساخت پاسخ نگه دار.
- منطق کسب‌وکار را در Service یا Domain Object قرار بده.
- Repository را فقط مسئول دسترسی به داده نگه دار.
- از Controller مستقیماً به Repository یا EntityManager دسترسی نده.
- از Repository سرویس دیگر، Queue یا سرویس خارجی را فراخوانی نکن.
- منطق کسب‌وکار را داخل DTO قرار نده.
- منطق عمومی و Stateless مشترک را در Utility متمرکز کن.
- منطق کسب‌وکار یا کد وابسته به Spring Bean را داخل Utility قرار نده.
- Design Pattern را فقط وقتی استفاده کن که مسئله واقعی آن Pattern وجود دارد.
- برای زنجیره شرط‌هایی که رفتار قابل توسعه را انتخاب می‌کنند، Strategy را در نظر بگیر.
- Abstraction غیرضروری ایجاد نکن.
- هر کلاس را روی یک مسئولیت متمرکز نگه دار.
- کلاس‌هایی با چند دلیل مستقل برای تغییر را تفکیک کن.
- وابستگی به پیاده‌سازی مشخص را در صورت نیاز به قابلیت تعویض یا تست، پشت Abstraction قرار بده.
- از زنجیره‌های طولانی مانند `a.getB().getC().getD()` پرهیز کن و جزئیات داخلی را پنهان کن.
- تغییر وضعیت Domain Object را از طریق متدهای دامنه انجام بده، نه Setter عمومی.

## 6. سرویس‌های خارجی

- برای هر فراخوانی سرویس خارجی، قبل از ارسال درخواست Log ثبت کن.
- نتیجه موفق فراخوانی سرویس خارجی را Log کن.
- پاسخ ناموفق، کد خطای HTTP و Exception ارتباطی را Log کن.
- شناسه یکتای برگشتی سرویس خارجی را در صورت وجود Log کن.
- داده حساس را در Log سرویس خارجی ثبت نکن.
- برای تمام فراخوانی‌های خارجی Connection Timeout تنظیم کن.
- برای تمام فراخوانی‌های خارجی Read/Response Timeout تنظیم کن.
- Timeout بسیار بزرگ یا نامحدود تعریف نکن.

## 7. Entity، JPA و Database

- همه Entityها را از Base Entity استاندارد پروژه با فیلدهای `version`، `createdAt`، `updatedAt`، `createdBy`، `updatedBy` و `removed` بهره‌مند کن.
- برای Optimistic Lock از `@Version` استفاده کن.
- خطای Optimistic Lock را به پاسخ مشخص مانند Conflict نگاشت کن.
- Pessimistic Lock را فقط زمانی استفاده کن که تعارض پرتکرار و شکست عملیات غیرقابل‌قبول است.
- حذف رکورد را به‌صورت Logical Delete انجام بده.
- همه Queryهای خواندن را طوری بنویس که رکوردهای `removed` را برنگردانند.
- Unique Indexهای مرتبط با Logical Delete را با درنظرگرفتن `removed` تعریف کن.
- وقتی فقط چند فیلد لازم است، کل Entity را Select نکن؛ از Projection/DTO استفاده کن.
- برای رکورد جدید از `persist` استفاده کن.
- روی Entity مدیریت‌شده `merge` فراخوانی نکن.
- `merge` را فقط برای Entity واقعاً Detached استفاده کن و از شیء برگشتی آن ادامه بده.
- زمان‌ها را به‌صورت `Instant` و UTC ذخیره کن.
- Queryها را خوانا و هر بخش منطقی را در خط جداگانه بنویس.
- در Java 15+ برای Queryهای چندخطی از Text Block استفاده کن.
- مقدار ورودی را با String Concatenation وارد Query نکن؛ همیشه Parameter Binding استفاده کن.
- نام Column/Table/Sort Field ورودی را Parameter فرض نکن؛ آن را با Whitelist اعتبارسنجی کن.
- Sequence را با نام، generator و `allocationSize` کامل تعریف کن.
- `allocationSize` را با `INCREMENT BY` دیتابیس هماهنگ نگه دار.
- برای Query جدید، نیاز به Index مناسب را بررسی کن.
- ترتیب ستون‌های Composite Index را با الگوی فیلتر Query هماهنگ کن.

## 8. Transaction

- Transaction را در Service تعریف کن، نه در Controller یا Repository.
- Transaction را تا حد ممکن کوتاه نگه دار.
- فقط عملیات پایگاه داده را داخل Transaction نگه دار.
- پردازش سنگین را خارج از Transaction انجام بده.
- سرویس خارجی را داخل Transaction فراخوانی نکن.
- کار جانبی وابسته به موفقیت Commit را بعد از Commit اجرا کن.
- برای عملیات فقط‌خواندنی از `@Transactional(readOnly = true)` استفاده کن.
- در صورت استفاده از Checked Exception، رفتار Rollback را صریح تعریف کن.
- روی Self Invocation برای `@Transactional`، `@Async` و `@Cacheable` تکیه نکن؛ فراخوانی باید از Proxy عبور کند.

## 9. Concurrency و Threading

- Collection معمولی مانند `HashMap` یا `ArrayList` را برای نوشتن همزمان چند Thread استفاده نکن.
- برای عملیات اتمیک از APIهای اتمیک مانند `computeIfAbsent` و `putIfAbsent` استفاده کن.
- وضعیت قابل تغییر Request را در فیلد Service یا Controller نگه ندار.
- Beanهای Spring را Stateless نگه دار و وابستگی‌های آن‌ها را `final` تعریف کن.
- محدوده Lock را تا حد ممکن کوچک نگه دار.
- فراخوانی شبکه یا Database را داخل Lock انجام نده.
- در سیستم چند Instance، برای همگام‌سازی بین Instanceها از `synchronized` استفاده نکن؛ از Distributed Lock استفاده کن.
- Lock را همیشه در `finally` آزاد کن.
- پس از گرفتن `InterruptedException`، با `Thread.currentThread().interrupt()` وضعیت Interrupt را برگردان.
- برای Executor از Queue با اندازه محدود استفاده کن.
- اندازه Pool و Queue را از Property بخوان.
- برای Threadها نام معنادار تعیین کن.
- سیاست Rejection Executor را آگاهانه انتخاب و مستند کن.
- کاری که از دست رفتنش قابل‌قبول نیست را در Queue پایدار یا Storage قابل‌بازیابی نگه دار.
- اگر نتیجه Task لازم نیست، به‌جای `submit` از `execute` استفاده کن.
- اگر از `submit` استفاده می‌کنی، حتماً Future را بررسی کن.
- `Future.get()` را بدون Timeout فراخوانی نکن.
- در `CompletableFuture` همیشه Executor اختصاصی بده.
- برای `CompletableFuture` Timeout و Error Handling تعریف کن.
- Context و MDC را هنگام انتقال کار به Thread دیگر منتقل کن.
- Context و MDC را در `finally` پاک کن.

## 10. Message Consumer و Async Processing

- اگر ترتیب پیام‌ها اهمیت دارد، پردازش آن‌ها را سریالی نگه دار.
- اگر ترتیب فقط به‌ازای یک کلید اهمیت دارد، Parallelism را با Partition/Grouping همان کلید طراحی کن.
- برای افزایش همزمانی Consumer، ابتدا Concurrency خود Listener را افزایش بده.
- پیام Queue را بدون دلیل مشخص به Executor داخلی واگذار نکن.
- پردازش پیام را طوری انجام بده که Acknowledge قبل از موفقیت واقعی پردازش انجام نشود.
- اگر پردازش Async باعث از بین رفتن Redelivery می‌شود، مکانیزم Recovery صریح پیاده‌سازی کن.
- برای Job غیرهمزمان REST، پاسخ `202 Accepted` و شناسه قابل پیگیری برگردان.
- Job غیرهمزمان قابل‌از‌دست‌رفتن را فقط در Queue حافظه‌ای Executor نگه ندار.

## 11. Exception و Error Handling

- برای خطاهای کسب‌وکار یک Base Exception مشترک تعریف کن.
- برای هر خطای کسب‌وکار Code ثابت و ماشین‌خوان تعریف کن.
- Exception را در Controller با `try/catch` به Response تبدیل نکن.
- نگاشت Exception به HTTP Response را در `@RestControllerAdvice` متمرکز کن.
- Stack Trace، SQL، نام Table، Constraint و پیام خام Exception را به Client برنگردان.
- خطا را با HTTP 200 برنگردان.
- Status Code را متناسب با نوع خطا انتخاب کن و در کل پروژه یکسان نگه دار.
- برای خطاهای فنی از Unchecked Exception استفاده کن.
- هنگام Wrap کردن Exception، Exception اصلی را به‌عنوان `cause` حفظ کن.
- Exception باید Context کافی برای تشخیص عملیات شکست‌خورده داشته باشد.
- خطاهای کتابخانه شخص ثالث را در Exception دامنه/مرزی مناسب Wrap کن.
- Exception را بی‌صدا نبلع؛ اگر نادیده‌گرفتن خطا عمدی است، دلیل را Comment و سطح Log مناسب ثبت کن.
- یک Exception را هم Log و هم بدون تغییر Re-throw نکن؛ Log نهایی را در یک نقطه انجام بده.

## 12. REST API

- Resourceهای REST را با اسم جمع و lowercase نام‌گذاری کن.
- فعل عملیات را در URL قرار نده؛ از HTTP Method استفاده کن.
- برای نام چندبخشی Resource از kebab-case استفاده کن.
- شناسه Resource را در Path و Filterها را در Query Parameter قرار بده.
- Nesting را فقط وقتی استفاده کن که Parent برای شناسایی یا مالکیت Resource لازم است.
- `GET` نباید وضعیت سیستم را تغییر بدهد.
- `PUT` را برای جایگزینی کامل و `PATCH` را برای تغییر جزئی استفاده کن.
- Retry عملیات غیرIdempotent را بدون Request Id یکتا انجام نده.
- برای ایجاد Resource از `201 Created` استفاده کن.
- برای پردازش غیرهمزمان از `202 Accepted` استفاده کن.
- برای پاسخ موفق بدون Body از `204 No Content` استفاده کن.
- هر API فهرستی را Pagination کن.
- برای `size` سقف تعریف کن.
- Sort Field را با Whitelist اعتبارسنجی کن.
- Sort را فقط روی فیلدهایی مجاز کن که Index مناسب دارند.
- خروجی `Page` داخلی Spring را مستقیم به Client برنگردان؛ DTO پاسخ اختصاصی بساز.
- Breaking Change را با Version جدید API منتشر کن.
- نام فیلدهای JSON را camelCase نگه دار.
- تاریخ و زمان API را ISO-8601 و UTC ارسال کن.
- مقادیر پولی را در کوچک‌ترین واحد و بدون اعشار منتقل کن.
- Enum/Status را با مقدار رشته‌ای معنادار برگردان، نه عدد.

## 13. Logging

- سطح Log را بر اساس نیاز به اقدام انتخاب کن، نه صرفاً موفق یا ناموفق بودن عملیات.
- خطای کسب‌وکاری عادی را بی‌دلیل در سطح `ERROR` ثبت نکن.
- پیام Log را انگلیسی و با ساختار ثابت بنویس.
- برای مقادیر Log از `{}` استفاده کن؛ String Concatenation انجام نده.
- Exception را به‌عنوان آخرین آرگومان Log بده تا Stack Trace حفظ شود.
- برای هر Request یک `referenceId` یکتا تولید و در MDC نگه دار.
- `referenceId` سرویس را از Header ورودی کپی نکن؛ شناسه Caller را جداگانه نگه دار.
- MDC را در `finally` پاک کن.
- Password، Token، API Key و Credential را هرگز Log نکن.
- شماره کارت کامل، CVV2، رمز دوم و تاریخ انقضا را Log نکن.
- کد ملی، شماره حساب و شماره موبایل را کامل Log نکن؛ Mask کن.
- Request/Response کامل را در سطح INFO ثبت نکن.
- داخل Loop حجیم برای هر رکورد Log ننویس؛ خلاصه شروع/پایان و خطاها را ثبت کن.

## 14. Clean Code و Formatting

- منطق تکراری را در یک نقطه متمرکز کن.
- از Magic Number استفاده نکن؛ Constant معنادار تعریف کن.
- هر متد را کوچک و روی یک کار متمرکز نگه دار.
- متدی با تعداد زیاد پارامتر را بازطراحی کن؛ در صورت نیاز از Object پارامتر استفاده کن.
- Side Effect پنهان ایجاد نکن.
- مدیریت خطا را از منطق اصلی متد جدا نگه دار.
- Comment را جایگزین کد واضح نکن.
- Comment باید «چرایی» را توضیح بدهد، نه «چه کاری» را.
- Comment قدیمی، اشتباه یا کد Comment‌شده را حذف کن.
- برای API عمومی در صورت نیاز JavaDoc بنویس.
- هر خط را به یک مفهوم اختصاص بده.
- Blockهای منطقی را با فاصله مناسب از هم جدا کن.
- متغیر را نزدیک محل استفاده تعریف کن.
- از خط‌های بسیار بلند پرهیز کن.
- Indentation و فاصله‌گذاری را یکدست رعایت کن.
- Import بلااستفاده را حذف کن.
- از Wildcard Import استفاده نکن.
- ترتیب اعضای کلاس را ثابت نگه دار: Constants، Fields، Constructors، Public/Protected Methods و سپس Private Methods.

## 15. Test

- هر Bug Fix و هر Feature جدید باید Test متناظر داشته باشد.
- هر Test فقط یک رفتار یا مفهوم را بررسی کند.
- نام Test باید رفتار مورد انتظار را بیان کند.
- Test را مستقل از ترتیب اجرای سایر Testها بنویس.
- Test را به شبکه، ساعت سیستم یا داده از پیش موجود وابسته نکن.
- برای زمان از `Clock` قابل تزریق استفاده کن.
- هر Test داده موردنیاز خودش را ایجاد کند.
- Test را به شناسه ثابت و از پیش موجود وابسته نکن.
- داده تست را پس از اجرا Rollback یا Cleanup کن.
- برای Integration Test دیتابیس، رفتار دیتابیس واقعی را مبنا قرار بده؛ به تفاوت دیتابیس In-Memory با Production بی‌توجه نباش.


## 16. Notise Project
- برای هر API یا endpoint جدید که در controller ها تعریف می شود، وجود متد متناظر برای فراخوانی آن را در کلاس Rest-over-Async-xx-handler مربوطه الزامی بررسی کن. اگر این متد در همان Pull Request اضافه نشده است، حتی اگر کلاس Rest-over-Async خارج از diff باشد، آن را به‌عنوان نقض قانون شرکت با شدت حداقل `warning` گزارش کن و کامنت را روی خط endpoint جدید قرار بده.
- برای هر entity جدید که تعریف میکنی حتما برای فیلد id نام و اطلاعات sequence generator را تعریف کن.
