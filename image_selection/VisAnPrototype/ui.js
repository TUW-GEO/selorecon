// UI BUTTONS

// TIME MODE BUTTON
timeModeButton = {
    name: "DATA",
    id: 'timeModeButton',
    pos: [5,5],
    r: 4,
    draw: function () {
      push(), noStroke(), fill(100);
      textAlign(RIGHT);
      text(this.name, this.pos[0]- this.r*3, height-3.5);
      ellipse(this.pos[0], this.pos[1], this.r*2), pop();
    },
    click: function () {
      timeMode = (timeMode === 'chronological'? 'fan':'chronological');
    }
  }

// FINISH BUTTON
finishButton = {
    name: "USER",
    id: 'finishButton',
    pos: [0,0],
    r: 4,
    draw: function () {
      push(), noStroke(), fill(urColor(1));
      textAlign(RIGHT);
      text(this.name, this.pos[0]- this.r*3, height-3.5);
      ellipse(this.pos[0], this.pos[1], this.r*2), pop();
    },
    click: function () {
      log.write('FINISH',null,null);
      console.log("***LOG START***");
      console.log(JSON.stringify(log.log));
      console.log("SELECTED");
      console.log(JSON.stringify(aerials.filter( a => a.meta.selected).map(a => a.id)));
      console.log("PRESCRIBED");
      console.log(JSON.stringify(prGuidance.prescribed.map(a => a.id)));
      log.write('***LOG END***','','');
    }
  }

// ORIENTING BUTTON
orButton = {
    name: "ORIENTING",
    id: 'orButton',
    pos: [0,0],
    r: 4,
    draw: function () {
      push(), noStroke(), fill(orColor(1));
      textAlign(RIGHT);
      text(this.name, this.pos[0]- this.r*3, height-3.5);
      ellipse(this.pos[0], this.pos[1], this.r*2), pop();
    },
    click: function () {
      orientingOn = !orientingOn;
      log.write('orienting'+(orientingOn?'On':'Off'),'','');
    }
  }

// PRESCRIBING BUTTON
prButton = {
    name: "PRESCRIBING",
    id: 'prButton',
    pos: [0,0],
    r: 4,
    draw: function () {
      push(), noStroke(), fill(prColor(1));
      textAlign(RIGHT);
      text(this.name, this.pos[0]- this.r*3, height-3.5);
      ellipse(this.pos[0], this.pos[1], this.r*2), pop();
    },
    click: function () {
      prescribingOn = !prescribingOn;
      log.write('prescribing'+(prescribingOn?'On':'Off'),'','');
    }
  }

// Image Stats for Buttons
const drawStats = function () {
    push(), translate(0, height-2);
    noStroke(), fill(50);

    let x = 20;
    push(), fill(100), textFont("Helvetica"), textStyle(BOLD), textSize(10), text("DoRIAH",1.5,-1.5), pop();
    timeModeButton.pos = [x+=120, height-7]; text(aerials.length+"/"+attackDates.length,x+=10,-1.5); 
    finishButton.pos = [x+=180, height-7]; text(aerials.filter( a => a.meta.selected).length+"/"+attacks.filter(a => a.coverage>0).length,x+=10,-1.5);
    prButton.pos = [x+=180, height-7]; text(prescribingOn?prGuidance.prescribed.length+"/"+attacks.filter(a => a.prescribed>0).length:0,x+=10,-1.5);
    orButton.pos = [x+=180, height-7];
    pop();
}

// IMAGE TOOLTIP
const drawTooltip = function (a) {
    let w = 200, h = 130;
    let x = 8, y = 16;
    let aerialMargin = a.vis.r+1;
    let lineSpace = 14;
    
    push(), translate(a.vis.pos[0]+aerialMargin,a.vis.pos[1]+aerialMargin);
    if (a.vis.pos[0] > width/2) translate(-w-aerialMargin, 0);
    if (a.vis.pos[1] > height/2) translate(0, -h-aerialMargin);
    fill('white'), stroke(200), strokeWeight(1);
    rect(0,0,w,h,0,15,15,15);
    
    fill('black'), textSize(11), textAlign(LEFT), textFont('Helvetica'), textStyle(BOLD), noStroke();
    text(a.meta.Sortie+'/'+a.meta.Bildnr, x, y)

    
    
    textStyle(NORMAL);
    text("Datum: "+a.meta.Datum, x, y += lineSpace);
    text("This "+ a.type +" image...", x, y += lineSpace);
    text("is " +( a.owned?"":"not ") + "owned by LBDB", x, y += lineSpace);
    text("has a "+Math.round(a.meta.Cvg,2)*100+"% coverage over the AOI", x, y += lineSpace);
    text("has a scale of "+a.meta.MASSTAB, x, y += lineSpace);
    text("is "+(a.usage==2?"selected":(a.usage==1?"not set":"discarded")), x, y += lineSpace);
    if (a.meta.pairs.length > 0) text("can be paired", x, y += lineSpace);
    if (orientingOn) {
        fill(orColor(1));
        // text("has a local interest value of "+Math.round(a.interest.post,2), x, y += lineSpace);
        // text("has a SQM value of "+a.meta.value, x, y += lineSpace);
        // text("has a normalized SQM value of "+a.meta.valueNormalized, x, y += lineSpace);
        // text("has a global interest value of "+Math.round(a.interest.pre,2), x, y += lineSpace);
    }
    if (prescribingOn) {
        fill(a.meta.prescribed?prColor(1):0);
        text("is "+( a.meta.prescribed?"":"not ") +"being prescribed", x, y += lineSpace);
    }
    pop();
}

const drawDateTooltip = function() {
  push();
  // Convert milliseconds to a Date object
  let date = new Date(a.date);
  // Format the date as "DD.MM.YYYY"
  let formattedDateString = date.toLocaleDateString('en-GB', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric'
  });

  textFont('Helvetica');
  textAlign(LEFT);
  noStroke(), fill(0);
  text(0, mouseX, h[0]+14);
  pop();
 }

 // TODO Attack Tooltip
 