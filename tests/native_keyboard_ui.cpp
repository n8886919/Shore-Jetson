// Real native key event routing against a local simulated PWM backend.
#define main shore_desktop_main
#include "../native/main.cpp"
#undef main

int main(int argc,char **argv) {
    QApplication app(argc,argv);
    if(argc!=3)return 2;
    Desktop desktop(QUrl(argv[1]),false);desktop.show();desktop.activateWindow();
    auto key=[](QWidget *target,QEvent::Type type,int code,bool repeat=false){
        QKeyEvent event(type,code,Qt::NoModifier,QString(),repeat);
        QApplication::sendEvent(target,&event);
    };
    QTimer::singleShot(800,[&]{key(&desktop,QEvent::KeyPress,Qt::Key_Right);});
    QTimer::singleShot(1100,[&]{key(&desktop,QEvent::KeyRelease,Qt::Key_Right,true);});
    QTimer::singleShot(1200,[&]{key(&desktop,QEvent::KeyPress,Qt::Key_Right,true);});
    QTimer::singleShot(3200,[&]{key(&desktop,QEvent::KeyRelease,Qt::Key_Right);});
    auto mark=[&](const char *value){QFile file(argv[2]);if(!file.open(QIODevice::WriteOnly))qFatal("phase file");file.write(value);};
    QTimer::singleShot(3350,[&]{
        desktop.showDrawer();auto speed=desktop.findChild<QDoubleSpinBox*>("manualSpeed");
        if(!speed || !speed->isEnabled())qFatal("manual speed unavailable");
        speed->setFocus();auto edit=speed->findChild<QLineEdit*>();edit->selectAll();mark("typing");
        QKeyEvent input(QEvent::KeyPress,Qt::Key_2,Qt::NoModifier,"2");
        QApplication::sendEvent(edit,&input); // No Enter, Return, blur or editingFinished.
    });
    bool speedApplied=false;
    QTimer phaseTimer;phaseTimer.setInterval(20);
    QObject::connect(&phaseTimer,&QTimer::timeout,[&]{
        QFile file(argv[2]);if(!file.open(QIODevice::ReadOnly) || file.readAll()!="applied")return;
        phaseTimer.stop();speedApplied=true;
        auto slider=desktop.findChild<QSlider*>();
        slider->setFocus();key(slider,QEvent::KeyPress,Qt::Key_Up);key(slider,QEvent::KeyRelease,Qt::Key_Up);
        QTimer::singleShot(400,[&]{desktop.setFocus();key(&desktop,QEvent::KeyPress,Qt::Key_Up);});
        QTimer::singleShot(700,[&]{QEvent event(QEvent::WindowDeactivate);QApplication::sendEvent(&desktop,&event);});
        QTimer::singleShot(1100,[&]{app.quit();});
    });
    phaseTimer.start();
    QTimer::singleShot(10000,[&]{if(!speedApplied)qFatal("speed requires Enter or focus loss");});
    return app.exec();
}
