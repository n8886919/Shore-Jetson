// Qt 5.9+ native desktop. Shares backend state and one volatile BGRx frame.
#include <QApplication>
#include <QWidget>
#include <QPushButton>
#include <QScrollArea>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QCheckBox>
#include <QSlider>
#include <QLineEdit>
#include <QAbstractSpinBox>
#include <QDoubleSpinBox>
#include <QComboBox>
#include <QLabel>
#include <QMessageBox>
#include <QKeyEvent>
#include <QCloseEvent>
#include <QPainter>
#include <QImage>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QNetworkInterface>
#include <QJsonObject>
#include <QJsonArray>
#include <QJsonDocument>
#include <QFile>
#include <QTimer>
#include <QElapsedTimer>
#include <QCommandLineParser>
#include <QSignalBlocker>
#include <QDebug>
#include <QLockFile>
#include <QDir>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#include <ctime>
#include <cstring>
#include <cmath>
#include <functional>

static double monotonicNow() { timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec+t.tv_nsec/1e9; }

static QString webAddress(const QString &host,int port) {
    return port==80?host:host+":"+QString::number(port);
}

static QIcon controlIcon(int kind) {
    QPixmap canvas(40,40);canvas.fill(Qt::transparent);QPainter p(&canvas);
    p.setRenderHint(QPainter::Antialiasing);p.setPen(QPen(QColor("#ecf7f5"),2.6,Qt::SolidLine,Qt::RoundCap,Qt::RoundJoin));
    if(kind==0){p.drawLine(9,9,31,31);p.drawLine(31,9,9,31);}
    else if(kind==1){p.drawLine(8,27,32,27);}
    else {
        QPolygonF gear;
        for(int i=0;i<32;++i){double angle=i*2*3.141592653589793/32;double r=(i%4==0||i%4==3)?17:13;gear<<QPointF(20+r*cos(angle),20+r*sin(angle));}
        p.drawPolygon(gear);p.drawEllipse(QPointF(20,20),5.5,5.5);
    }
    return QIcon(canvas);
}

class Toggle : public QCheckBox {
public:
    using QCheckBox::QCheckBox;
    QSize sizeHint() const override {return QSize(200,44);}
protected:
    bool hitButton(const QPoint &point) const override {return rect().contains(point);}
    void paintEvent(QPaintEvent*) override {
        QPainter p(this);p.setRenderHint(QPainter::Antialiasing);QFont font("Noto Sans CJK TC");font.setPixelSize(19);p.setFont(font);
        const double scale=qMin(1.0,height()/44.0);p.scale(scale,scale);
        const double y=(height()/scale-30)/2;
        p.setPen(QPen(isEnabled()?QColor("#a8c4ca"):QColor("#728991"),1.5));
        p.setBrush(isChecked()?QColor("#34c759"):QColor("#345560"));p.drawRoundedRect(QRectF(1,y,50,30),15,15);
        p.setPen(Qt::NoPen);p.setBrush(isEnabled()?QColor("#ffffff"):QColor("#bacbd1"));
        p.drawEllipse(QPointF(isChecked()?36:16,y+15),11,11);
        p.setPen(isEnabled()?QColor("#ecf7f5"):QColor("#adc1c5"));
        p.drawText(QRectF(64,0,width()/scale-64,height()/scale),Qt::AlignLeft|Qt::AlignVCenter,text());
        if(hasFocus()){p.setPen(QPen(QColor("#29d6bd"),2));p.setBrush(Qt::NoBrush);p.drawRoundedRect(QRectF(0,0,width()/scale-1,height()/scale-1),5,5);}
    }
};

class Desktop : public QWidget {
    QNetworkAccessManager network;
    QTimer statusTimer, frameTimer, addressTimer, testTimer, keyboardTimer;
    int heldKey=0, keyboardRevision=0, keyboardSequence=0;
    QString keyboardSession, keyboardEpoch;
    bool keyboardBusy=false;
    QElapsedTimer statusAge;
    QJsonObject state;
    QJsonObject testState;
    QByteArray testToken;
    QString testSession;
    int testSequence=0, testRevision=0;
    bool testBusy=false, testStopping=false, testPolling=false;
    QString testError;
    QElapsedTimer testPollAge, testAge, recoveryAge;
    QFile frameFile;
    QImage image;
    quint64 sequence=0, displayed=0;
    double imageTime=0;
    QElapsedTimer inferencePulse;
    int lastInference=-1;
    bool offline=false, drawer=false, compact=false, pending=false, statusPending=false;
    QString cameraInterface="eth0", address="網路未連線", notice;
    QUrl base;
    QPushButton *arm, *gear, *shrink, *exitButton, *testButton;
    QScrollArea *panel;
    QCheckBox *tilt, *continuous, *inferenceEnabled;
    QSlider *deadband, *zoom;
    QDoubleSpinBox *manualSpeed;
    bool manualSpeedBusy=false;
    double queuedManualSpeed=-1;
    QLabel *deadbandText, *zoomText, *feedback, *testNotice, *recoveryNotice, *recordingCapacity;
    QList<QWidget*> lens;
    void request(QString path, QJsonObject body, std::function<void(QJsonObject,bool)> done, bool post=false) {
        QNetworkRequest req(base.resolved(QUrl(path)));
        req.setRawHeader("Origin", base.toString(QUrl::RemovePath|QUrl::RemoveQuery|QUrl::RemoveFragment).toUtf8());
        req.setHeader(QNetworkRequest::ContentTypeHeader,"application/json");
        if (post && (path=="/api/servo-test" || path=="/api/keyboard")) req.setRawHeader("X-Pan-Tilt-Token",testToken);
        auto reply=post ? network.post(req,QJsonDocument(body).toJson(QJsonDocument::Compact)) : network.get(req);
        auto timeout=new QTimer(reply); timeout->setSingleShot(true);
        connect(timeout,&QTimer::timeout,reply,&QNetworkReply::abort); timeout->start(2500);
        connect(reply,&QNetworkReply::finished,this,[=] {
            const auto data=QJsonDocument::fromJson(reply->readAll()).object();
            const bool ok=reply->error()==QNetworkReply::NoError;
            reply->deleteLater(); done(data,ok);
        });
    }
    QString keyDirection(int key) const {
        if(key==Qt::Key_Up)return "up";if(key==Qt::Key_Down)return "down";
        if(key==Qt::Key_Left)return "left";if(key==Qt::Key_Right)return "right";return {};
    }
    bool keyboardReady() const {
        return !offline && !pending && fresh() && statusAge.elapsed()<1500
            && !testToken.isEmpty() && state["keyboard"].toObject()["enabled"].toBool();
    }
    void stopKeyboard() {
        if(!heldKey)return;
        const QString epoch=keyboardEpoch;
        heldKey=0;keyboardSession.clear();++keyboardRevision;keyboardBusy=false;
        // Wait for status readback before allowing a new start with another epoch.
        auto k=state["keyboard"].toObject();k["enabled"]=false;state["keyboard"]=k;
        request("/api/keyboard",{{"action","stop"},{"epoch",epoch}},[=](QJsonObject,bool){pollStatus();},true);
    }
    void startKeyboard(int key) {
        if(heldKey || !keyboardReady())return;
        heldKey=key;keyboardEpoch=state["keyboard"].toObject()["epoch"].toString();
        keyboardSession.clear();keyboardSequence=0;
        const int revision=++keyboardRevision;
        request("/api/keyboard",{{"action","start"},{"epoch",keyboardEpoch},{"direction",keyDirection(key)}},
            [=](QJsonObject r,bool ok){
                if(revision!=keyboardRevision)return;
                if(!ok || !r["ok"].toBool()){stopKeyboard();return;}
                keyboardSession=r["session"].toString();
            },true);
    }
    void renewKeyboard() {
        if(!heldKey)return;
        if(!keyboardReady() || !isActiveWindow()
                || state["keyboard"].toObject()["epoch"].toString()!=keyboardEpoch){stopKeyboard();return;}
        if(keyboardBusy || keyboardSession.isEmpty())return;
        keyboardBusy=true;const int revision=keyboardRevision;
        request("/api/keyboard",{{"action","move"},{"session",keyboardSession},
            {"sequence",++keyboardSequence},{"direction",keyDirection(heldKey)}},[=](QJsonObject r,bool ok){
                if(revision!=keyboardRevision)return;
                keyboardBusy=false;if(!ok || !r["ok"].toBool())stopKeyboard();
            },true);
    }
    bool armed() const { auto t=state["tracking"].toObject(); return t["armed"].toBool() || t["mode"].toString()=="arming"; }
    bool fresh() const { return statusAge.isValid() && statusAge.elapsed()<3000; }
    bool testing() const {return testBusy || !testSession.isEmpty() || testState["running"].toBool();}
    void renderTest() {
        const bool known=testAge.isValid() && testAge.elapsed()<3000;
        const QString phase=testState["phase"].toString();
        const QString step=testStopping?"停止中":testBusy?"啟動中":phase=="center"?"置中":
            phase=="left"?"左 20°":phase=="right"?"右 40°":phase=="up"?"抬頭 20°":phase=="down"?"低頭 20°":"測試中";
        testButton->setText(testing()?step+"\n停止測試":"雲台測試");
        testButton->setMinimumHeight(qRound((testing()?64:46)*qMin(width()/1024.0,height()/600.0)));
        testButton->setStyleSheet(testing()?"background:#712b35;color:white;border-color:#ad5965":"");
        testButton->setEnabled(!offline && !testToken.isEmpty() && (testing() ||
            (known && testState["available"].toBool() && testState["calibrated"].toBool()
             && !armed() && !testState["tracking_active"].toBool() && !testState["connected"].toBool())));
        const QString detail=!testError.isEmpty()?testError:!testState["error"].toString().isEmpty()?testState["error"].toString():
            phase=="stopped" && testState["notice"].toString().contains("逾時")?testState["notice"].toString():QString();
        testNotice->setText(offline?"離線預覽不執行舵機動作":!detail.isEmpty()?detail:!known?"等待舵機測試狀態":
            testState["tracking_active"].toBool()?"請先停止自動跟拍":
            !testState["available"].toBool()?"目前設定不支援 PWM 測試":
            !testState["calibrated"].toBool()?"請先設定水平 M0、垂直 M1 並確認兩軸方向":QString());
        testNotice->setVisible(!testNotice->text().isEmpty());
    }
    void stopTest() {
        if (!testing() || offline || testToken.isEmpty()) return;
        const int revision=++testRevision; testSession.clear(); testBusy=true; testStopping=true; renderTest();
        request("/api/servo-test",{{"action","stop"}},[=](QJsonObject r,bool ok){
            if(revision!=testRevision)return;
            testBusy=false;testStopping=false;
            if(ok && r["ok"].toBool()){testState=r["status"].toObject();testAge.restart();}
            else {testAge.invalidate();testError=r["detail"].toString("停止回覆中斷；系統會因測試連線逾時停送");}
            renderTest();pollStatus();
        },true);
    }
    void toggleTest() {
        if(testing()){stopTest();return;}
        if(!drawer || !testButton->isEnabled())return;
        const int revision=++testRevision;testBusy=true;testStopping=false;testError.clear();renderTest();
        request("/api/servo-test",{{"action","start"},{"epoch",testState["epoch"]}},[=](QJsonObject r,bool ok){
            if(revision!=testRevision)return;
            if(!ok || !r["ok"].toBool()){
                stopTest();testError=r["detail"].toString("無法啟動測試，請檢查服務連線");renderTest();return;
            }
            testBusy=false;testState=r["status"].toObject();testSession=r["session"].toString();
            testSequence=0;testAge.restart();renderTest();pollStatus();
        },true);
    }
    void pollTest() {
        if(offline || testBusy || testPolling || (testSession.isEmpty() && testPollAge.isValid() && testPollAge.elapsed()<1000))return;
        const int revision=testRevision;const bool heartbeat=!testSession.isEmpty();
        testPolling=true;testPollAge.restart();
        QJsonObject body=heartbeat?QJsonObject{{"action","heartbeat"},{"session",testSession},{"sequence",++testSequence}}:QJsonObject{};
        request("/api/servo-test",body,[=](QJsonObject r,bool ok){
            testPolling=false;if(revision!=testRevision || testBusy)return;
            if(!ok || (heartbeat && !r["ok"].toBool())){
                stopTest();testAge.invalidate();testError=r["detail"].toString("測試連線中斷，等待服務恢復");renderTest();return;
            }
            testState=heartbeat?r["status"].toObject():r;
            if(!r["control_token"].toString().isEmpty())testToken=r["control_token"].toString().toUtf8();
            if(!testState["running"].toBool())testSession.clear();
            testAge.restart();renderTest();
        },heartbeat);
    }
    void command(QString path,QJsonObject values={}) {
        if (offline || pending) return;
        pending=true; refreshButtons();
        request(path,values,[=](QJsonObject r,bool ok) {
            pending=false; notice=ok ? QString() : r["detail"].toString("服務連線失敗");
            feedback->setText(notice); refreshButtons(); pollStatus();
        },true);
    }
    void applyTuning() {
        if (offline || !fresh() || state["tuning"].toObject().isEmpty()) return;
        auto t=state["tuning"].toObject();
        command("/api/tuning",{{"pan_gain_deg",t["pan_gain_deg"]},{"pan_direction",t["pan_direction"]},
            {"pan_deadband",deadband->value()/200.0},{"zoom_maximum",zoom->value()/10.0},{"tilt_enabled",tilt->isChecked()}});
    }
    void sendManualSpeed() {
        if(manualSpeedBusy || queuedManualSpeed<.1 || offline || !fresh() || testToken.isEmpty())return;
        const double value=queuedManualSpeed;queuedManualSpeed=-1;manualSpeedBusy=true;
        request("/api/keyboard",{{"action","speed"},{"speed_deg_s",value}},[=](QJsonObject r,bool ok){
            manualSpeedBusy=false;
            const auto keyboard=r["status"].toObject();
            const bool applied=ok && r["ok"].toBool() && keyboard.contains("speed_deg_s")
                && qFuzzyCompare(1+keyboard["speed_deg_s"].toDouble(),1+value);
            if(applied)state["keyboard"]=keyboard;
            feedback->setText(applied?"已套用":r["detail"].toString("速度未套用，請重試"));
            if(!applied)queuedManualSpeed=-1;
            sendManualSpeed();refreshButtons();pollStatus();
        },true);
    }
    QPushButton* button(QString text,QVBoxLayout *layout,std::function<void()> action) {
        auto b=new QPushButton(text); b->setMinimumHeight(38); layout->addWidget(b);
        connect(b,&QPushButton::clicked,this,action); return b;
    }
    void refreshButtons() {
        const bool active=armed(), known=fresh();
        arm->setText(!known && !offline ? "停止追蹤" : active ? "停止追蹤" : "開始追蹤");
        arm->setEnabled(!offline && !pending && (!known || active || state["arm_allowed"].toBool()));
        arm->setStyleSheet(QString(active || (!known && !offline) ? "background:#712b35;color:white;font-size:%1px" : "background:#29d6bd;color:#05211e;font-size:%1px").arg(qMax(12,int(22*qMin(width()/1024.0,height()/600.0)))));
        tilt->setEnabled(!offline && known && !pending && state["tuning"].toObject()["tilt_available"].toBool());
        const bool cameraCard=state["manual_recording"].toObject()["destination"].toString()=="camera_sd";
        continuous->setVisible(!cameraCard);
        continuous->setEnabled(!cameraCard && !offline && known && !pending);
        const auto disk=state["metrics"].toObject();
        const auto used=disk["used_gib"], free=disk["free_gib"];
        recordingCapacity->setVisible(state["manual_recording"].toObject()["destination"].toString()!="camera_sd");
        recordingCapacity->setText(known && !offline && used.isDouble() && free.isDouble()
            ? QString("錄影空間：已用 %1 GiB／剩餘 %2 GiB").arg(used.toDouble(),0,'f',1).arg(free.toDouble(),0,'f',1)
            : QString("錄影空間：尚無資料"));
        const auto ai=state["inference"].toObject();
        const auto aiState=state["components"].toObject()["inference"].toObject()["state"].toString();
        inferenceEnabled->setEnabled(!offline && known && !pending && ai.contains("enabled") && !ai["locked"].toBool()
            && (aiState=="idle" || aiState=="ready" || aiState=="stale"));
        inferenceEnabled->setToolTip(ai["locked"].toBool()?"錄影期間保持開啟":"AI 辨識");
        deadband->setEnabled(!offline && known && !pending); zoom->setEnabled(deadband->isEnabled());
        const auto keyboard=state["keyboard"].toObject();
        manualSpeed->setEnabled(!offline && known && !pending && !testToken.isEmpty()
            && keyboard["available"].toBool() && keyboard.contains("speed_deg_s"));
        for (auto w:lens) w->setEnabled(!offline && known && !active && !pending);
        renderTest();
    }
    void pollStatus() {
        if (offline || statusPending) return;
        statusPending=true;
        if(!recoveryAge.isValid() || recoveryAge.elapsed()>=1000){
            recoveryAge.restart();
            request("/api/diagnostics",{},[=](QJsonObject d,bool ok){
                const QString recovery=d["recovery"].toObject()["state"].toString();
                recoveryNotice->setText(!ok?"等待服務恢復連線":recovery=="queued"?"等待重新連接相機…":
                    recovery=="reconnecting"?"正在重新連接相機…":recovery=="waiting"?"等待相機影像恢復…":
                    recovery=="blocked"?"影像資源尚未釋放，系統會繼續重試":"");
            });
        }
        request("/api/status",{},[=](QJsonObject data,bool ok){
            statusPending=false;
            if (ok) {
                state=data; statusAge.restart();
                int result=state["recognition"].toObject()["sequence"].toInt(-1);
                if(result>=0 && result!=lastInference){lastInference=result;inferencePulse.restart();}
                auto t=state["tuning"].toObject();
                QSignalBlocker a(tilt),b(continuous),c(deadband),d(zoom),e(inferenceEnabled);
                QSignalBlocker speedBlock(manualSpeed);
                const auto keyboard=state["keyboard"].toObject();
                manualSpeed->setMaximum(keyboard["max_speed_deg_s"].toDouble(5));
                const auto focus=QApplication::focusWidget();
                const bool editing=manualSpeed->hasFocus() || (focus && manualSpeed->isAncestorOf(focus));
                if(!editing && !manualSpeedBusy && queuedManualSpeed<0 && keyboard.contains("speed_deg_s"))
                    manualSpeed->setValue(keyboard["speed_deg_s"].toDouble());
                inferenceEnabled->setChecked(state["inference"].toObject()["enabled"].toBool());
                tilt->setChecked(t["tilt_enabled"].toBool());
                continuous->setChecked(state["manual_recording"].toObject()["continuous_enabled"].toBool());
                if (!deadband->isSliderDown()) deadband->setValue(qRound(t["pan_deadband"].toDouble(.2)*200));
                if (!zoom->isSliderDown()) zoom->setValue(qRound(t["zoom_maximum"].toDouble(1)*10));
                deadbandText->setText(QString("跟拍容許區 %1%").arg(deadband->value()));
                zoomText->setText(QString("畫面放大 %1×").arg(zoom->value()/10.0,0,'f',1));
            }
            refreshButtons(); update();
        });
        if (!frameFile.isOpen()) request("/api/local-display",{},[=](QJsonObject d,bool ok){
            if (!ok || frameFile.isOpen()) return;
            cameraInterface=d["camera_interface"].toString("eth0");
            QString path=d["path"].toString();
            if (!path.startsWith("/dev/shm/shore-display-")) return;
            frameFile.setFileName(path);
            if (frameFile.open(QIODevice::ReadOnly)) {
                struct stat st;
                if (fstat(frameFile.handle(),&st)!=0 || st.st_uid!=getuid() || (st.st_mode&077)!=0 || st.st_size!=64+960*540*4) frameFile.close();
            }
        });
    }
    void readFrame() {
        if (offline) { update(); return; }
        if (!frameFile.isOpen()) return;
        struct stat st;
        if (fstat(frameFile.handle(),&st)!=0 || st.st_nlink==0) {frameFile.close(); sequence=0; return;}
        if (flock(frameFile.handle(),LOCK_SH|LOCK_NB)!=0) return;
        frameFile.seek(0); QByteArray header=frameFile.read(64);
        quint32 w=0,h=0,stride=0; quint64 seq=0; double captured=0;
        if (header.size()==64 && header.left(8)==QByteArray("SHORE01\0",8)) {
            memcpy(&w,header.constData()+8,4); memcpy(&h,header.constData()+12,4);
            memcpy(&stride,header.constData()+16,4); memcpy(&seq,header.constData()+24,8); memcpy(&captured,header.constData()+32,8);
            const double age=monotonicNow()-captured;
            if (seq!=sequence && w>0 && w<=960 && h>0 && h<=540 && stride>=w*4 && stride<=3840 && std::isfinite(age) && age>=0 && age<.6) {
                const auto bytes=frameFile.read(stride*h);
                if (bytes.size()==int(stride*h)) {
                    image=QImage(reinterpret_cast<const uchar*>(bytes.constData()),w,h,stride,QImage::Format_RGB32).copy();
                    sequence=seq; imageTime=captured; ++displayed;
                }
            }
        }
        flock(frameFile.handle(),LOCK_UN); refreshButtons(); update();
    }
    void refreshAddress() {
        QStringList addresses;
        for (const auto &iface:QNetworkInterface::allInterfaces()) {
            if (iface.name()==cameraInterface || !(iface.flags()&QNetworkInterface::IsUp) || iface.flags()&QNetworkInterface::IsLoopBack) continue;
            for (const auto &entry:iface.addressEntries()) {
                auto ip=entry.ip(); quint32 n=ip.toIPv4Address();
                if ((n>>24==10) || (n>>20==0xac1) || (n>>16==0xc0a8)) addresses << webAddress(ip.toString(),base.port(80));
            }
        }
        address=addresses.isEmpty()?QString("網路未連線"):addresses.join("  "); update();
    }
    void positionWidgets() {
        double s=qMin(width()/1024.0,height()/600.0); int content=drawer?768:960;
        auto place=[s](QWidget *w,int x,int y,int a,int b){w->setGeometry(qRound(x*s),qRound(y*s),qRound(a*s),qRound(b*s));};
        place(arm,content-156,552,144,40); place(gear,967,122,50,50);
        place(exitButton,967,6,50,50); place(shrink,967,64,50,50); place(panel,768,0,256,544);
        gear->setVisible(!drawer); exitButton->setVisible(!drawer); shrink->setVisible(!drawer); panel->setVisible(drawer);
        for (auto b:{gear,shrink,exitButton}) b->setIconSize(QSize(qRound(38*s),qRound(38*s)));
        panel->setStyleSheet(QString("QWidget{font-size:%1px}").arg(qMax(10,int(19*s))));
        tilt->setFixedHeight(qRound(44*s));continuous->setFixedHeight(qRound(44*s));inferenceEnabled->setFixedHeight(qRound(44*s));
        for(auto b:panel->findChildren<QPushButton*>())b->setMinimumHeight(qRound(46*s));
        refreshButtons();
    }
protected:
    bool event(QEvent *e) override {
        if(e->type()==QEvent::WindowDeactivate || e->type()==QEvent::Hide){stopTest();stopKeyboard();}
        return QWidget::event(e);
    }
    bool eventFilter(QObject *watched,QEvent *event) override {
        auto w=qobject_cast<QWidget*>(watched);
        if(!w || w->window()!=this)return QWidget::eventFilter(watched,event);
        const bool input=qobject_cast<QSlider*>(w) || qobject_cast<QLineEdit*>(w)
            || qobject_cast<QAbstractSpinBox*>(w) || qobject_cast<QComboBox*>(w);
        if(event->type()==QEvent::FocusIn && input)stopKeyboard();
        if(event->type()==QEvent::KeyPress || event->type()==QEvent::KeyRelease){
            auto key=static_cast<QKeyEvent*>(event);
            if(key->key()==Qt::Key_Escape)stopKeyboard();
            if(event->type()==QEvent::KeyRelease && key->key()==heldKey){
                if(!key->isAutoRepeat())stopKeyboard();
                return true;
            }
            if(event->type()==QEvent::KeyPress && !keyDirection(key->key()).isEmpty() && !input
                    && !(key->modifiers()&(Qt::AltModifier|Qt::ControlModifier|Qt::MetaModifier))){
                if(heldKey)return true;
                if(keyboardReady() && !key->isAutoRepeat()){startKeyboard(key->key());return true;}
            }
        }
        return QWidget::eventFilter(watched,event);
    }
    void resizeEvent(QResizeEvent*) override {positionWidgets();}
    void keyPressEvent(QKeyEvent *e) override {if(e->key()==Qt::Key_Escape){stopTest();drawer=false;positionWidgets();update();}else QWidget::keyPressEvent(e);}
    void mousePressEvent(QMouseEvent*) override {if(drawer){stopTest();drawer=false;positionWidgets();update();}}
    void closeEvent(QCloseEvent *e) override {
        stopTest();stopKeyboard();
        if (offline || QMessageBox::question(this,"關閉 Shore Jetson","關閉視窗？背景追蹤與錄影會繼續執行。",QMessageBox::Yes|QMessageBox::No,QMessageBox::No)==QMessageBox::Yes) e->accept(); else e->ignore();
    }
    void paintEvent(QPaintEvent*) override {
        QPainter p(this);p.fillRect(rect(),Qt::black);const double s=qMin(width()/1024.0,height()/600.0);p.scale(s,s);
        const int content=drawer?768:960;
        QRectF video(0,0,content,content*9.0/16.0);
        const bool visible=!image.isNull() && (offline || monotonicNow()-imageTime<.6);
        if (visible) {
            auto metrics=state["metrics"].toObject(); double z=metrics["zoom_scale"].toDouble(1),cx=metrics["zoom_center_x"].toDouble(.5);
            z=qBound(1.0,z,6.0);cx=qBound(.5/z,cx,1-.5/z);
            QRectF src((cx-.5/z)*image.width(),(.5-.5/z)*image.height(),image.width()/z,image.height()/z);
            p.drawImage(video,image,src);p.save();p.setClipRect(video);
            auto tx=[&](double x){return video.left()+((x-cx)*z+.5)*video.width();};auto ty=[&](double y){return ((y-.5)*z+.5)*video.height();};
            auto tuning=state["tuning"].toObject();double db=tuning["pan_deadband"].toDouble(.2);
            p.setPen(QPen(QColor("#29d6bd"),1));for(double x:{.5-db,.5+db})p.drawLine(QPointF(tx(x),0),QPointF(tx(x),video.height()));
            if(tuning["tilt_enabled"].toBool())for(double y:{.5-db,.5+db})p.drawLine(QPointF(video.left(),ty(y)),QPointF(video.right(),ty(y)));
            auto recognition=state["recognition"].toObject();
            if(fresh() && state["inference"].toObject()["enabled"].toBool() && recognition["available"].toBool() && statusAge.elapsed()<recognition["valid_for_ms"].toInt()) {
                p.setFont(QFont("Noto Sans CJK TC",12));
                for(auto item:recognition["detections"].toArray()) {
                    auto obj=item.toObject(),b=obj["box"].toObject();
                    QRectF box(QPointF(tx(b["x"].toDouble()),ty(b["y"].toDouble())),QPointF(tx(b["x"].toDouble()+b["width"].toDouble()),ty(b["y"].toDouble()+b["height"].toDouble())));
                    p.setPen(QPen(QColor("#29d6bd"),2));p.drawRect(box);p.drawText(box.topLeft()+QPointF(2,18),QString("ID %1").arg(obj["track_id"].toInt()));
                }
            }
            p.restore();
        } else {
            p.fillRect(video,QColor("#10242b")); p.setPen(QColor("#adc1c5")); p.setFont(QFont("Noto Sans CJK TC",20));
            p.drawText(video,Qt::AlignCenter,offline?"離線預覽":"等待相機畫面");
        }
        p.resetTransform();p.scale(s,s);p.setClipping(false);
        p.fillRect(QRectF(0,544,1024,56),QColor("#0a171d"));p.setFont(QFont("Noto Sans CJK TC",12));p.setPen(QColor("#ecf7f5"));
        auto metrics=state["metrics"].toObject(),rec=state["manual_recording"].toObject();
        QString recordingLabel=rec["state"].toString()=="recording"?"錄影中":"未錄影";
        if (rec["destination"].toString()=="camera_sd") {
            const auto scheduled=rec["schedule_enabled"];
            recordingLabel=!scheduled.isBool()?"相機卡排程未知":scheduled.toBool()?"相機卡排程已開":"相機卡排程已關";
        }
        QString text=offline?"離線預覽":!fresh()?"服務連線中斷":QString("%1 FPS    %2").arg(qRound(metrics["preview_fps"].toDouble())).arg(recordingLabel);
        if(armed()&&fresh() && state["keyboard"].toObject()["enabled"].toBool())text += "    方向鍵長按移動，放開停止";
        else if(armed()&&fresh())text += state["tracking"].toObject()["mode"].toString()=="searching"?"    尋找中":rec["tracking_qualified"].toBool()?"    起乘／衝浪":"    偵測到追浪";
        if(fresh() && !offline){auto servo=state["components"].toObject()["servo"].toObject()["state"].toString();
            if(servo=="manual")text+="    舵機手動控制中";else if(servo!="ready")text+="    舵機未連線";}
        if(!notice.isEmpty()) text=notice;
        p.drawText(QRectF(12,547,770,23),Qt::AlignLeft|Qt::AlignVCenter,text);
        if(fresh() && state["inference"].toObject()["enabled"].toBool() && state["recognition"].toObject()["available"].toBool()) {
            p.setPen(Qt::NoPen);p.setBrush(inferencePulse.isValid()&&inferencePulse.elapsed()<160?QColor("#29d6bd"):QColor("#07562b"));
            p.drawEllipse(QPointF(788,560),6,6);
        }
        p.setFont(QFont("Noto Sans CJK TC",10));p.setPen(QColor("#adc1c5"));p.drawText(QRectF(12,574,770,22),Qt::AlignLeft|Qt::AlignVCenter,offline?webAddress("192.0.2.10",base.port(80))+"  ·  離線示意":address);
    }
public:
    Desktop(QUrl url,bool preview):offline(preview),base(url) {
        setWindowTitle("Shore Jetson");setObjectName("ShoreTracker");resize(1024,600);setMinimumSize(512,300);
        setStyleSheet("QWidget{background:#000;color:#ecf7f5;font-family:'Noto Sans CJK TC';font-size:15px} QPushButton{background:#192e34;border:1px solid #38545b;border-radius:7px;padding:4px} QPushButton:disabled{color:#62777b} QPushButton:focus{border:2px solid #29d6bd} QSlider::groove:horizontal{height:6px;background:#38545b} QSlider::handle:horizontal{background:#29d6bd;width:20px;margin:-8px 0;border-radius:9px} QScrollArea{border:0} QScrollBar:vertical{background:#101c21;width:10px} QScrollBar::handle:vertical{background:#607b84;min-height:30px;border-radius:5px} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0} QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:none}");
        arm=new QPushButton("開始追蹤",this);gear=new QPushButton(this);shrink=new QPushButton(this);exitButton=new QPushButton(this);
        gear->setIcon(controlIcon(2));gear->setAccessibleName("設定");
        shrink->setIcon(controlIcon(1));shrink->setAccessibleName("縮小或還原視窗");
        exitButton->setIcon(controlIcon(0));exitButton->setAccessibleName("關閉視窗");
        panel=new QScrollArea(this);panel->setWidgetResizable(true);panel->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);auto controls=new QWidget;auto layout=new QVBoxLayout(controls);layout->setContentsMargins(14,14,14,14);layout->setSpacing(10);
        layout->addWidget(new QLabel("鏡頭"));
        for(auto row:QList<QList<QPair<QString,QString>>>{
                {{"近焦","focus_near"},{"遠焦","focus_far"}},
                {{"放大","zoom_in"},{"縮小","zoom_out"}}}) {
            auto pair=new QHBoxLayout;pair->setSpacing(8);layout->addLayout(pair);
            for(auto entry:row) {
                auto b=new QPushButton(entry.first);b->setMinimumHeight(46);pair->addWidget(b);lens<<b;
                connect(b,&QPushButton::clicked,this,[=]{command("/api/camera",{{"action",entry.second},{"values",QJsonObject{}}});});
            }
        }
        testButton=button("雲台測試",layout,[=]{toggleTest();});testButton->setObjectName("servoTestButton");
        testNotice=new QLabel;testNotice->setObjectName("servoTestState");testNotice->setWordWrap(true);layout->addWidget(testNotice);
        inferenceEnabled=new Toggle("AI 辨識");inferenceEnabled->setObjectName("inferenceEnabled");layout->addWidget(inferenceEnabled);
        connect(inferenceEnabled,&QCheckBox::clicked,this,[=](bool v){stopKeyboard();command("/api/inference",{{"enabled",v}});});
        auto speedLabel=new QLabel("手動控制速度（°/s）");speedLabel->setWordWrap(true);layout->addWidget(speedLabel);
        manualSpeed=new QDoubleSpinBox;manualSpeed->setObjectName("manualSpeed");
        manualSpeed->setAccessibleName("手動控制速度（°/s）");manualSpeed->setRange(.1,5);
        manualSpeed->setDecimals(1);manualSpeed->setSingleStep(.1);manualSpeed->setValue(5);
        manualSpeed->setKeyboardTracking(true);manualSpeed->setMinimumHeight(36);
        manualSpeed->setStyleSheet("QDoubleSpinBox{background:#13252a;border:1px solid #38545b;border-radius:7px;padding:5px 9px}");
        layout->addWidget(manualSpeed);speedLabel->setBuddy(manualSpeed);
        connect(manualSpeed,QOverload<double>::of(&QDoubleSpinBox::valueChanged),this,[=](double value){
            if(!manualSpeed->isEnabled())return;
            queuedManualSpeed=value;sendManualSpeed();
        });
        tilt=new Toggle("啟用俯仰");layout->addWidget(tilt);connect(tilt,&QCheckBox::clicked,this,[=]{applyTuning();});
        deadbandText=new QLabel("跟拍容許區 40%");layout->addWidget(deadbandText);deadband=new QSlider(Qt::Horizontal);deadband->setRange(10,80);deadband->setValue(40);layout->addWidget(deadband);
        connect(deadband,&QSlider::valueChanged,this,[=](int v){deadbandText->setText(QString("跟拍容許區 %1%").arg(v));});connect(deadband,&QSlider::sliderReleased,this,[=]{applyTuning();});
        zoomText=new QLabel("畫面放大 1.0×");layout->addWidget(zoomText);zoom=new QSlider(Qt::Horizontal);zoom->setRange(10,40);zoom->setValue(10);layout->addWidget(zoom);
        connect(zoom,&QSlider::valueChanged,this,[=](int v){zoomText->setText(QString("畫面放大 %1×").arg(v/10.0,0,'f',1));});connect(zoom,&QSlider::sliderReleased,this,[=]{applyTuning();});
        continuous=new Toggle("連續錄影");layout->addWidget(continuous);connect(continuous,&QCheckBox::clicked,this,[=](bool v){command("/api/recording/mode",{{"continuous",v}});});
        recordingCapacity=new QLabel; recordingCapacity->setWordWrap(true); layout->addWidget(recordingCapacity);
        recoveryNotice=new QLabel;recoveryNotice->setObjectName("recoveryState");recoveryNotice->setWordWrap(true);layout->addWidget(recoveryNotice);
        feedback=new QLabel;feedback->setWordWrap(true);layout->addWidget(feedback);layout->addStretch();panel->setWidget(controls);
        connect(gear,&QPushButton::clicked,this,[=]{if(drawer)stopTest();drawer=!drawer;positionWidgets();update();});connect(exitButton,&QPushButton::clicked,this,[=]{close();});
        connect(shrink,&QPushButton::clicked,this,[=]{compact=!compact;if(compact){showNormal();resize(512,300);}else showFullScreen();});
        connect(arm,&QPushButton::clicked,this,[=]{stopKeyboard();drawer=false;positionWidgets();command(!fresh()||armed()?"/api/stop":"/api/arm");});
        connect(&statusTimer,&QTimer::timeout,this,[=]{pollStatus();});statusTimer.start(250);
        connect(&frameTimer,&QTimer::timeout,this,[=]{readFrame();});frameTimer.start(20);
        connect(&addressTimer,&QTimer::timeout,this,[=]{refreshAddress();});addressTimer.start(5000);refreshAddress();
        qApp->installEventFilter(this);
        connect(&keyboardTimer,&QTimer::timeout,this,[=]{renewKeyboard();});keyboardTimer.start(100);
        connect(&testTimer,&QTimer::timeout,this,[=]{pollTest();});testTimer.start(250);pollTest();
        if(offline){image=QImage(960,540,QImage::Format_RGB32);QPainter p(&image);QLinearGradient grad(0,0,960,540);grad.setColorAt(0,QColor("#123c48"));grad.setColorAt(1,QColor("#08141c"));p.fillRect(image.rect(),grad);p.setPen(QColor("#adc1c5"));p.setFont(QFont("Noto Sans CJK TC",24));p.drawText(image.rect(),Qt::AlignCenter,"Shore Jetson · 離線預覽");}
        refreshButtons();pollStatus();positionWidgets();
    }
    quint64 frameCount() const {return displayed;}
    void showDrawer(){drawer=true;positionWidgets();update();}
    void showCompact(){compact=true;showNormal();resize(512,300);}
};

int main(int argc,char **argv) {
    QApplication app(argc,argv);app.setApplicationName("ShoreTracker");app.setApplicationDisplayName("Shore Jetson");app.setOrganizationName("ShoreTracker");app.setDesktopFileName("shore-tracker");
    QCommandLineParser parser;parser.addHelpOption();parser.addOption({"url","Local backend URL","url","http://127.0.0.1:80"});parser.addOption({"offline","Offline layout preview (no commands)"});parser.addOption({"controls","Open the settings drawer"});parser.addOption({"compact","Open at 512x300"});parser.addOption({"windowed","Open a normal window"});parser.addOption({"screenshot","Save an offline-only layout screenshot","path"});parser.addOption({"smoke-seconds","Exit after a bounded display check","seconds"});parser.process(app);
    QUrl url(parser.value("url"));if(url.scheme()!="http" || !(url.host()=="127.0.0.1"||url.host()=="localhost"||url.host()=="::1"))qFatal("Desktop backend must be loopback HTTP");
    if(parser.isSet("screenshot")&&!parser.isSet("offline"))qFatal("Screenshots require --offline");
    QLockFile lock(QDir::tempPath()+QString("/shore-tracker-desktop-%1.lock").arg(getuid()));
    if(!parser.isSet("offline")&&!parser.isSet("smoke-seconds")&&!lock.tryLock(100)) {qInfo("Shore Jetson desktop is already running");return 0;}
    Desktop desktop(url,parser.isSet("offline"));if(parser.isSet("windowed")||parser.isSet("screenshot"))desktop.show();else desktop.showFullScreen();
    if(parser.isSet("controls"))desktop.showDrawer();
    if(parser.isSet("compact"))desktop.showCompact();
    if(parser.isSet("screenshot"))QTimer::singleShot(500,[&]{desktop.grab().save(parser.value("screenshot"));app.quit();});
    if(parser.isSet("smoke-seconds"))QTimer::singleShot(parser.value("smoke-seconds").toInt()*1000,[&]{qInfo("DISPLAYED_FRAMES=%llu",(unsigned long long)desktop.frameCount());app.quit();});
    return app.exec();
}
